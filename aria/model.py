"""Training model implementation."""

from dataclasses import dataclass
from typing import Optional

import torch
import torch.utils.checkpoint

from torch import nn as nn
from torch.nn import functional as F


@dataclass
class ModelConfig:
    d_model: int
    n_heads: int
    n_layers: int
    ff_mult: int
    drop_p: float
    max_seq_len: int
    grad_checkpoint: bool
    resid_dropout: float = 0.0
    vocab_size: Optional[int] = None
    class_size: Optional[int] = None
    emb_size: Optional[dict] = None

    def set_vocab_size(self, vocab_size: int):
        self.vocab_size = vocab_size


class FusedEncoderBlock(nn.Module):
    def __init__(self, model_config: ModelConfig, resid_dropout: float = 0.0):
        super().__init__()
        self.drop_p = model_config.drop_p
        self.n_heads = model_config.n_heads
        self.d_head = model_config.d_model // model_config.n_heads
        self.max_seq_len = model_config.max_seq_len
        self.resid_dropout = resid_dropout

        # Attention
        self.mixed_qkv = nn.Linear(
            in_features=model_config.d_model,
            out_features=3 * model_config.d_model,
            bias=False,
        )
        self.att_proj_linear = nn.Linear(
            in_features=model_config.d_model,
            out_features=model_config.d_model,
            bias=False,
        )

        # FF Layer
        self.ff_gate_proj = nn.Linear(
            in_features=model_config.d_model,
            out_features=model_config.d_model * model_config.ff_mult,
            bias=False,
        )
        self.ff_up_proj = nn.Linear(
            in_features=model_config.d_model,
            out_features=model_config.d_model * model_config.ff_mult,
            bias=False,
        )
        self.ff_down_proj = nn.Linear(
            in_features=model_config.d_model * model_config.ff_mult,
            out_features=model_config.d_model,
            bias=False,
        )

        # Pre layer norms
        self.norm1 = nn.LayerNorm(model_config.d_model)
        self.norm2 = nn.LayerNorm(model_config.d_model)

    def forward(self, x: torch.Tensor, freqs_cis: torch.Tensor):
        att_out = self._att_block(self.norm1(x), freqs_cis)
        x = x + F.dropout(att_out, p=self.resid_dropout, training=self.training)

        ff_out = self._ff_block(self.norm2(x))
        x = x + F.dropout(ff_out, p=self.resid_dropout, training=self.training)

        return x

    def _att_block(self, x: torch.Tensor, freqs_cis: torch.Tensor):
        batch_size, seq_len, _ = x.shape
        mixed_qkv = self.mixed_qkv(x)
        xq, xk, xv = mixed_qkv.chunk(3, -1)

        # Reshape for rotary embeddings
        # Need contiguous for q, k since in-place RoPE cannot be applied on a view
        xq = xq.reshape(
            batch_size, seq_len, self.n_heads, self.d_head
        ).contiguous()
        xk = xk.reshape(
            batch_size, seq_len, self.n_heads, self.d_head
        ).contiguous()
        xv = xv.view(batch_size, seq_len, self.n_heads, self.d_head)

        # apply_rotary_post_emb expects: (b_sz, s_len, n_head, d_head)
        xq = apply_rotary_emb(xq, freqs_cis)
        xk = apply_rotary_emb(xk, freqs_cis)
        xq, xk, xv = map(lambda t: t.transpose(1, 2), (xq, xk, xv))

        # scaled_dot_product_attention expects: (b_sz, n_head, s_len, d_head)
        att = F.scaled_dot_product_attention(
            query=xq,
            key=xk,
            value=xv,
            is_causal=True,
        )

        # Reshape for out: (b_sz, s_len, n_head, d_head)
        out = att.transpose(1, 2).contiguous()
        out = out.view(batch_size, seq_len, self.n_heads * self.d_head)

        return self.att_proj_linear(out)

    def _ff_block(self, x: torch.Tensor):

        return self.ff_down_proj(
            F.silu(self.ff_gate_proj(x)) * self.ff_up_proj(x)
        )


class Transformer(nn.Module):
    """Transformer decoder without a language model head.

    Args:
        model_config (ModelConfig): Model configuration settings.
    """

    def __init__(self, model_config: ModelConfig):
        super().__init__()
        self.model_config = model_config
        self.freqs_cis = None

        self.tok_embeddings = nn.Embedding(
            num_embeddings=model_config.vocab_size,
            embedding_dim=model_config.d_model,
        )

        self.out_layer_norm = nn.LayerNorm(model_config.d_model)
        self.encode_layers = nn.ModuleList()

        for layer_index in range(model_config.n_layers):
            if model_config.resid_dropout > 0:
                layer_dropout = model_config.resid_dropout * (
                    layer_index / (model_config.n_layers - 1)
                )
            else:
                layer_dropout = 0.0

            self.encode_layers.append(
                FusedEncoderBlock(model_config, resid_dropout=layer_dropout)
            )

    def forward(
        self,
        src: torch.Tensor,
        emb: torch.Tensor | None = None,
    ):
        """Perform a forward pass through the transformer.

        Args:
            src (torch.Tensor): Input tensor of token indices with shape (batch_size, seq_len).
            emb (Optional[torch.Tensor]): Optional extra embedding with shape (batch_size, emb_dim).

        Returns:
            torch.Tensor: Output tensor with shape (batch_size, seq_len, d_model).
        """

        hidden_states = self.tok_embeddings(src)

        if emb is not None:
            emb = emb[:, None, :]
            hidden_states = torch.cat([emb, hidden_states[:, :-1, :]], dim=1)

        if self.freqs_cis is None:
            self.freqs_cis = precompute_freqs_cis(
                seq_len=self.model_config.max_seq_len,
                n_elem=self.model_config.d_model // self.model_config.n_heads,
                base=500000,
            ).to(src.device)
        freqs_cis = self.freqs_cis[: src.shape[1]]

        if self.model_config.grad_checkpoint is True and self.training:
            for layer in self.encode_layers:

                def create_custom_forward(module):
                    def custom_forward(*args):
                        return module(*args)

                    return custom_forward

                hidden_states = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(layer),
                    hidden_states,
                    freqs_cis,
                    preserve_rng_state=True,
                    use_reentrant=True,
                )
        else:
            for layer in self.encode_layers:
                hidden_states = layer(hidden_states, freqs_cis=freqs_cis)

        return self.out_layer_norm(hidden_states)


class TransformerLM(nn.Module):
    """Transformer decoder with a language modeling head.

    Args:
        model_config (ModelConfig): Model configuration settings (vocab_size must be defined).
    """

    def __init__(self, model_config: ModelConfig):
        super().__init__()
        assert model_config.vocab_size is not None

        self.max_seq_len = model_config.max_seq_len
        self.model = Transformer(model_config)
        self.lm_head = nn.Linear(
            model_config.d_model, model_config.vocab_size, bias=False
        )

    def forward(
        self,
        src: torch.Tensor,
    ):
        """Compute language modeling logits.

        Args:
            src (torch.Tensor): Input tensor of token indices with shape (batch_size, seq_len).

        Returns:
            torch.Tensor: Logits with shape (batch_size, seq_len, vocab_size).
        """

        hidden = self.model(src)
        logits = self.lm_head(hidden)

        return logits


class TransformerCL(nn.Module):
    """Transformer decoder with a classification head.

    Args:
        model_config (ModelConfig): Model configuration settings (class_size must be defined).
    """

    def __init__(self, model_config: ModelConfig):
        super().__init__()
        assert model_config.class_size is not None

        self.max_seq_len = model_config.max_seq_len
        self.model = Transformer(model_config)
        self.class_head = nn.Linear(
            model_config.d_model, model_config.class_size, bias=False
        )

    def forward(
        self,
        src: torch.Tensor,
    ):
        """Compute classification logits.

        Args:
            src (torch.Tensor): Input tensor of token indices with shape (batch_size, seq_len).

        Returns:
            torch.Tensor: Classification logits with shape (batch_size, seq_len, class_size).
        """

        hidden = self.model(src)
        logits = self.class_head(hidden)

        return logits


class TransformerLM_CND(nn.Module):
    """Transformer decoder with a language modeling head and optional conditioning.

    Args:
        model_config (ModelConfig): Model configuration settings (vocab_size and emb_size must be defined).
    """

    def __init__(self, model_config: ModelConfig):
        super().__init__()
        assert model_config.vocab_size is not None

        self.max_seq_len = model_config.max_seq_len
        self.model = Transformer(model_config)
        self.lm_head = nn.Linear(
            model_config.d_model, model_config.vocab_size, bias=False
        )
        self.embedding_adapter = nn.Linear(
            model_config.emb_size, model_config.d_model, bias=False
        )

    def forward(
        self,
        src: torch.Tensor,
        emb: torch.Tensor | None = None,
    ):
        """Compute language modeling logits with optional conditioning.

        Args:
            src (torch.Tensor): Input tensor of token indices with shape (batch_size, seq_len).
            emb (Optional[torch.Tensor]): Optional conditioning embedding with shape (batch_size, emb_size).

        Returns:
            torch.Tensor: Logits with shape (batch_size, seq_len, vocab_size).
                Note that if the emb is provided, the seq_len will be seq_len -1.

        """

        if emb is not None:
            # Embedding is prepended to sequence via the adapter. We slice the
            # logits so that the logits format still matches src.
            emb = self.embedding_adapter(emb)
            hidden = self.model(src, emb)
            logits = self.lm_head(hidden)

            return logits[:, 1:, :]
        else:
            # Needed for torch dpp error
            dummy_input = torch.zeros(
                src.size(0),
                self.embedding_adapter.in_features,
                device=src.device,
            )
            dummy_output = self.embedding_adapter(dummy_input)
            dummy_loss = dummy_output.sum() * 0.0

            hidden = self.model(src, None)
            logits = self.lm_head(hidden)
            logits = logits + dummy_loss

            return logits


class TransformerEMB(nn.Module):
    """Transformer decoder with an embedding head.

    Args:
        model_config (ModelConfig): Model configuration settings (emb_size must be defined).
    """

    def __init__(self, model_config: ModelConfig):
        super().__init__()
        assert model_config.emb_size is not None

        self.max_seq_len = model_config.max_seq_len
        self.model = Transformer(model_config)
        self.emb_head = nn.Linear(
            model_config.d_model, model_config.emb_size, bias=False
        )

    def forward(
        self,
        src: torch.Tensor,
    ):
        """Compute output embeddings from the transformer.

        Args:
            src (torch.Tensor): Input tensor of token indices with shape (batch_size, seq_len).

        Returns:
            torch.Tensor: Output embeddings with shape (batch_size, seq_len, emb_size).
        """

        hidden = self.model(src)
        emb = self.emb_head(hidden)

        return emb


def precompute_freqs_cis(
    seq_len: int,
    n_elem: int,
    base: int = 500000,
):
    freqs = 1.0 / (
        base ** (torch.arange(0, n_elem, 2)[: (n_elem // 2)].float() / n_elem)
    )
    t = torch.arange(seq_len, device=freqs.device)
    freqs = torch.outer(t, freqs)
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)
    cache = torch.stack([freqs_cis.real, freqs_cis.imag], dim=-1)

    return cache


@torch.jit.script
def apply_rotary_emb(x: torch.Tensor, freqs_cis: torch.Tensor) -> torch.Tensor:
    """
    In-place RoPE. Credits to Katherine Crowson:
    x shape (b_sz, s_len, n_head, d_head).
    freqs_cis shape (s_len, d_head // 2, 2) and is float32.
    """
    x_float = x.float()
    freqs_cis = freqs_cis.detach()
    d = x_float.shape[-1] // 2
    cos = freqs_cis[..., 0][None, :, None]
    sin = freqs_cis[..., 1][None, :, None]
    x1, x2 = x_float[..., :d], x_float[..., d : d * 2]
    tmp = x1.clone()
    x1.mul_(cos).addcmul_(x2, sin, value=-1)
    x2.mul_(cos).addcmul_(tmp, sin, value=1)
    return x.copy_(x_float)
