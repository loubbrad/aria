import os
import sys
import csv
import argparse
import logging
import random
import torch
import accelerate

from torch import nn as nn
from torch.utils.data import DataLoader

from accelerate.logging import get_logger
from safetensors.torch import load_file
from logging.handlers import RotatingFileHandler
from tqdm import tqdm
from typing import List

from aria.config import load_model_config
from aria.model import ModelConfig, TransformerLM, TransformerLM_CND
from ariautils.tokenizer import Tokenizer, AbsTokenizer, RelTokenizer
from aria.datasets import (
    TrainingDataset,
    PretrainingDataset,
)
from aria.utils import _load_weight

torch._dynamo.config.optimize_ddp = False


# ----- USAGE -----
#
# This script is meant to be run using the huggingface accelerate cli, see:
#
# https://huggingface.co/docs/accelerate/basic_tutorials/launch
# https://huggingface.co/docs/accelerate/package_reference/cli
#
# For example usage you could run the pre-training script with:
#
# accelerate launch [arguments] aria/train.py train \
#   medium \
#   --train_data data/train \
#   --val_data data/val \
#   --epochs 10 \
#   --bs 32 \
#   --workers 8
#
# You could resume a run from an accelerate checkpoint with:
#
# accelerate launch [arguments] aria/train.py resume \
#   medium \
#   --train_data data/train \
#   --val_data data/val \
#   --cp_dir models/epoch5_step0 \
#   --r_step 0 \
#   --r_epoch 5 \
#   --epochs 5 \
#   --bs 32 \
#   --workers 8


def setup_logger(project_dir: str):
    # Get logger and reset all handlers
    logger = logging.getLogger(__name__)
    for h in logger.handlers[:]:
        logger.removeHandler(h)

    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "[%(asctime)s] %(name)s: [%(levelname)s] %(message)s",
    )

    fh = RotatingFileHandler(
        os.path.join(project_dir, "logs.txt"), backupCount=5, maxBytes=1024**3
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return get_logger(__name__)  # using accelerate.logging.get_logger()


def get_tokenizer_name(
    train_data_paths: str,
    val_data_path: str,
):
    """This will throw an error if there is a tokenizer mismatch"""
    train_config = TrainingDataset.get_config_from_path(train_data_paths[0])
    val_config = TrainingDataset.get_config_from_path(val_data_path)

    assert (
        train_config["tokenizer_name"] == val_config["tokenizer_name"]
    ), "Dataset tokenizers don't match"

    return train_config["tokenizer_name"]


def setup_project_dir(project_dir: str | None):
    if not project_dir:
        # Create project directory
        if not os.path.isdir("./experiments"):
            os.mkdir("./experiments")

        project_dirs = [
            _dir
            for _dir in os.listdir("./experiments")
            if os.path.isdir(os.path.join("experiments", _dir))
        ]

        ind = 0
        while True:
            if str(ind) not in project_dirs:
                break
            else:
                ind += 1

        project_dir_abs = os.path.abspath(os.path.join("experiments", str(ind)))
        assert not os.path.isdir(project_dir_abs)
        os.mkdir(project_dir_abs)

    elif project_dir:
        # Run checks on project directory
        if os.path.isdir(project_dir):
            assert (
                len(os.listdir(project_dir)) == 0
            ), "Provided project directory is not empty"
            project_dir_abs = os.path.abspath(project_dir)
        elif os.path.isfile(project_dir):
            raise FileExistsError(
                "The provided path points toward an existing file"
            )
        else:
            try:
                os.mkdir(project_dir)
            except Exception as e:
                raise e(f"Failed to create project directory at {project_dir}")
        project_dir_abs = os.path.abspath(project_dir)

    os.mkdir(os.path.join(project_dir_abs, "checkpoints"))

    return project_dir_abs


def _get_optim(
    lr: float,
    model: nn.Module,
    num_epochs: int,
    steps_per_epoch: int,
    warmup: int = 100,
    end_ratio: float = 0.1,
):
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=lr,
        weight_decay=0.1,
        betas=(0.9, 0.95),
        eps=1e-5,
    )

    warmup_lrs = torch.optim.lr_scheduler.LinearLR(
        optimizer,
        start_factor=0.000001,
        end_factor=1,
        total_iters=warmup,
    )
    linear_decay_lrs = torch.optim.lr_scheduler.LinearLR(
        optimizer,
        start_factor=1,
        end_factor=end_ratio,
        total_iters=(num_epochs * steps_per_epoch) - warmup,
    )

    lr_scheduler = torch.optim.lr_scheduler.SequentialLR(
        optimizer,
        schedulers=[warmup_lrs, linear_decay_lrs],
        milestones=[warmup],
    )

    return optimizer, lr_scheduler


def get_optim(
    model: nn.Module,
    num_epochs: int,
    steps_per_epoch: int,
):
    LR = 3e-4
    END_RATIO = 0.1
    WARMUP_STEPS = 200

    return _get_optim(
        lr=LR,
        model=model,
        num_epochs=num_epochs,
        steps_per_epoch=steps_per_epoch,
        warmup=WARMUP_STEPS,
        end_ratio=END_RATIO,
    )


def get_dataloaders(
    train_data_dirs: List[str],
    val_data_dir: str,
    tokenizer: Tokenizer,
    batch_size: int,
    num_workers: int,
    use_embeddings: bool,
    init_epoch: int | None = None,
    apply_aug: bool = True,
):
    train_dataset = PretrainingDataset(
        dir_paths=train_data_dirs,
        tokenizer=tokenizer,
    )
    val_dataset = PretrainingDataset(
        dir_paths=val_data_dir,
        tokenizer=tokenizer,
    )

    if init_epoch:
        train_dataset.init_epoch(idx=init_epoch)

    assert (
        len(val_dataset.epoch_files_by_dir[0]) == 1
    ), "val-data directory should only contain one epoch"

    if apply_aug:
        train_dataset.set_transform(tokenizer.export_data_aug())

    train_dataloader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        num_workers=num_workers,
        shuffle=True,
    )
    val_dataloader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        num_workers=num_workers,
        shuffle=False,
    )

    if use_embeddings is True:
        _src, _tgt, _mask, _emb = train_dataset[0]
        _src, _tgt, _mask, __emb = val_dataset[0]
        assert _emb.numel() != 0, "Embeddings not present in train dataset"
        assert __emb.numel() != 0, "Embeddings not present in val dataset"

    return train_dataloader, val_dataloader


def _train(
    epochs: int,
    accelerator: accelerate.Accelerator,
    model: TransformerLM,
    train_dataloader: DataLoader,
    val_dataloader: DataLoader,
    use_embeddings: bool,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler = None,
    steps_per_checkpoint: int | None = None,
    resume_step: int | None = None,
    resume_epoch: int | None = None,
    project_dir: str | None = None,
):
    def make_checkpoint(
        _accelerator: accelerate.Accelerator, _epoch: int, _step: int
    ):
        if accelerator.is_main_process:
            checkpoint_dir = os.path.join(
                project_dir,
                "checkpoints",
                f"epoch{_epoch}_step{_step}",
            )

            logger.info(
                f"EPOCH {_epoch}/{epochs + start_epoch}: Saving checkpoint - {checkpoint_dir}"
            )
            _accelerator.save_state(checkpoint_dir)

    # This is all slightly messy as train_loop and val_loop make use of the
    # variables in the wider scope. Perhaps refactor this at some point.
    def train_loop(dataloader: DataLoader, _epoch: int, _resume_step: int = 0):
        loss = torch.tensor([0.0])
        avg_train_loss = 0
        trailing_loss = 0
        loss_buffer = []

        try:
            lr_for_print = "{:.2e}".format(scheduler.get_last_lr()[0])
        except Exception:
            pass
        else:
            lr_for_print = "{:.2e}".format(optimizer.param_groups[-1]["lr"])

        model.train()
        for __step, batch in (
            pbar := tqdm(
                enumerate(dataloader),
                total=len(dataloader) + _resume_step,
                initial=_resume_step,
                leave=False,
            )
        ):
            pbar.set_postfix_str(
                f"lr={lr_for_print}, "
                f"loss={round(loss.item(), 4)}, "
                f"trailing={round(trailing_loss, 4)}"
            )

            with accelerator.accumulate(model):
                step = __step + _resume_step + 1
                src, tgt, mask, emb = (
                    batch  # (b_sz, s_len), (b_sz, s_len), (b_sz, s_len), (b_sz, d_emb)
                )

                use_embeddings_cond = use_embeddings and (random.random() > 0.5)

                if use_embeddings_cond is True:
                    logits = model(src=src, emb=emb)  # (b_sz, s_len - 1, v_sz)
                    tgt = tgt[:, :-1]  # (b_sz, s_len - 1)
                    mask = mask[:, :-1]  # (b_sz, s_len - 1)
                else:
                    logits = model(src)  # (b_sz, s_len, v_sz)

                logits = logits.transpose(
                    1, 2
                )  # Transpose for CrossEntropyLoss
                loss = loss_fn(logits, tgt)

                if mask.sum() == 0:
                    loss = (loss * 0).sum()
                else:
                    loss = loss * mask
                    loss = loss[loss != 0.0].mean()

                # Calculate statistics
                loss_buffer.append(accelerator.gather(loss).mean(dim=0).item())
                trailing_loss = sum(loss_buffer[-TRAILING_LOSS_STEPS:]) / len(
                    loss_buffer[-TRAILING_LOSS_STEPS:]
                )
                avg_train_loss = sum(loss_buffer) / len(loss_buffer)

                # Logging
                logger.debug(
                    f"EPOCH {_epoch} STEP {step}: "
                    f"lr={lr_for_print}, "
                    f"loss={round(loss.item(), 4)}, "
                    f"trailing_loss={round(trailing_loss, 4)}, "
                    f"average_loss={round(avg_train_loss, 4)}"
                )

                if accelerator.is_main_process:
                    loss_writer.writerow([_epoch, step, loss.item()])

                accelerator.backward(loss)
                optimizer.step()
                optimizer.zero_grad()
                if scheduler:
                    scheduler.step()
                    lr_for_print = "{:.2e}".format(scheduler.get_last_lr()[0])

                if steps_per_checkpoint:
                    if step % steps_per_checkpoint == 0:
                        make_checkpoint(
                            _accelerator=accelerator,
                            _epoch=_epoch,
                            _step=step,
                        )

        logger.info(
            f"EPOCH {_epoch}/{epochs + start_epoch}: Finished training - "
            f"average_loss={round(avg_train_loss, 4)}"
        )

        return avg_train_loss

    @torch.no_grad()
    def val_loop(dataloader, _epoch: int):
        loss_buffer = []
        model.eval()
        for step, batch in (
            pbar := tqdm(
                enumerate(dataloader),
                total=len(dataloader),
                leave=False,
            )
        ):
            src, tgt, mask, emb = (
                batch  # (b_sz, s_len), (b_sz, s_len), (b_sz, s_len), (b_sz, d_emb)
            )
            use_embeddings_cond = use_embeddings and (random.random() > 0.5)

            if use_embeddings_cond is True:
                logits = model(src=src, emb=emb)  # (b_sz, s_len - 1, v_sz)
                tgt = tgt[:, :-1]  # (b_sz, s_len - 1)
                mask = mask[:, :-1]  # (b_sz, s_len - 1)
            else:
                logits = model(src)  # (b_sz, s_len, v_sz)

            logits = logits.transpose(1, 2)  # Transpose for CrossEntropyLoss
            loss = loss_fn(logits, tgt)

            if mask.sum() == 0:
                loss = (loss * 0).sum()
            else:
                loss = loss * mask
                loss = loss[loss != 0.0].mean()

            # Logging
            loss_buffer.append(accelerator.gather(loss).mean(dim=0).item())
            avg_val_loss = sum(loss_buffer) / len(loss_buffer)
            pbar.set_postfix_str(f"average_loss={round(avg_val_loss, 4)}")

        # EPOCH
        logger.info(
            f"EPOCH {_epoch}/{epochs + start_epoch}: Finished evaluation - "
            f"average_loss={round(avg_val_loss, 4)}"
        )

        return avg_val_loss

    if steps_per_checkpoint:
        assert (
            steps_per_checkpoint > 1
        ), "Invalid checkpoint mode value (too small)"

    TRAILING_LOSS_STEPS = 200
    PAD_ID = train_dataloader.dataset.tokenizer.pad_id
    logger = get_logger(__name__)  # Accelerate logger
    loss_fn = nn.CrossEntropyLoss(ignore_index=PAD_ID, reduction="none")

    logger.info(
        f"Model has "
        f"{'{:,}'.format(sum(p.numel() for p in model.parameters() if p.requires_grad))} "
        "parameters"
    )

    if accelerator.is_main_process:
        loss_csv = open(os.path.join(project_dir, "loss.csv"), "w")
        loss_writer = csv.writer(loss_csv)
        loss_writer.writerow(["epoch", "step", "loss"])
        epoch_csv = open(os.path.join(project_dir, "epoch.csv"), "w")
        epoch_writer = csv.writer(epoch_csv)
        epoch_writer.writerow(["epoch", "avg_train_loss", "avg_val_loss"])

    if resume_epoch is not None:
        start_epoch = resume_epoch + 1
    else:
        start_epoch = 0

    if resume_step is not None:
        assert resume_epoch is not None, "Must provide resume epoch"
        logger.info(
            f"Resuming training from step {resume_step} - logging as EPOCH {resume_epoch}"
        )
        skipped_dataloader = accelerator.skip_first_batches(
            dataloader=train_dataloader,
            num_batches=resume_step,
        )

        avg_train_loss = train_loop(
            dataloader=skipped_dataloader,
            _epoch=resume_epoch,
            _resume_step=resume_step,
        )
        avg_val_loss = val_loop(dataloader=val_dataloader, _epoch=resume_epoch)
        if accelerator.is_main_process:
            epoch_writer.writerow([resume_epoch, avg_train_loss, avg_val_loss])
            epoch_csv.flush()
            make_checkpoint(
                _accelerator=accelerator, _epoch=start_epoch, _step=0
            )

    for epoch in range(start_epoch, epochs + start_epoch):
        train_dataloader.dataset.init_epoch(epoch)
        avg_train_loss = train_loop(dataloader=train_dataloader, _epoch=epoch)
        avg_val_loss = val_loop(dataloader=val_dataloader, _epoch=epoch)
        if accelerator.is_main_process:
            epoch_writer.writerow([epoch, avg_train_loss, avg_val_loss])
            epoch_csv.flush()
            make_checkpoint(_accelerator=accelerator, _epoch=epoch + 1, _step=0)

    logging.shutdown()
    if accelerator.is_main_process:
        loss_csv.close()
        epoch_csv.close()


# TODO: Add use_embeddings logic to this code path
def resume_train(
    model_name: str,
    train_data_paths: str,
    val_data_path: str,
    use_embeddings: bool,
    num_workers: int,
    batch_size: int,
    grad_acc_steps: int,
    epochs: int,
    checkpoint_dir: str,
    resume_epoch: int,
    resume_step: int,
    steps_per_checkpoint: int | None = None,
    project_dir: str = None,
):
    # Validate inputs
    assert 0 < num_workers <= 128, "Too many workers"
    assert epochs > 0, "Invalid number of epochs"
    assert batch_size > 0, "Invalid batch size"
    assert torch.cuda.is_available() is True, "CUDA not available"
    assert os.path.isdir(checkpoint_dir), f"No dir at {checkpoint_dir}"
    for train_data_path in train_data_paths:
        assert os.path.isdir(
            train_data_path
        ), f"No dir found at {train_data_path}"
    assert os.path.isdir(val_data_path), f"No dir found at {val_data_path}"

    tokenizer_name = get_tokenizer_name(train_data_paths, val_data_path)
    if tokenizer_name == "abs":
        tokenizer = AbsTokenizer()
    elif tokenizer_name == "rel":
        tokenizer = RelTokenizer()
    else:
        raise Exception("Invalid tokenizer name")

    accelerator = accelerate.Accelerator(
        project_dir=project_dir, gradient_accumulation_steps=grad_acc_steps
    )
    if accelerator.is_main_process:
        project_dir = setup_project_dir(project_dir)
        logger = setup_logger(project_dir)

    logger = get_logger(__name__)
    logger.info(f"Using project directory {project_dir} ")
    logger.warning(
        "Please insure that the training config and resume step are set "
        "correctly, the script does not currently check that this is the case. "
        "If the previous checkpoint was saved at step n, then resume_step "
        "should be n. If there is a mismatch between the batch size then the "
        "script will resume at the wrong step. It is also important that the "
        "same distributed setup is used for training."
    )
    logger.info(
        f"Using training config: "
        f"model_name={model_name}, "
        f"use_embeddings={use_embeddings}, "
        f"epochs={epochs}, "
        f"batch_size={batch_size}, "
        f"grad_acc_steps={grad_acc_steps}, "
        f"num_workers={num_workers}, "
        f"checkpoint_dir={checkpoint_dir}, "
        f"resume_step={resume_step}, "
        f"resume_epoch={resume_epoch}"
    )

    if steps_per_checkpoint:
        logger.info(f"Creating checkpoints every {steps_per_checkpoint}")

    # Init model
    model_config = ModelConfig(**load_model_config(model_name))
    model_config.set_vocab_size(tokenizer.vocab_size)

    if use_embeddings:
        model = TransformerLM_CND(model_config)
    else:
        model = TransformerLM(model_config)

    model.compile()

    train_dataloader, val_dataloader = get_dataloaders(
        train_data_dirs=train_data_paths,
        val_data_dir=val_data_path,
        tokenizer=tokenizer,
        init_epoch=resume_epoch,
        batch_size=batch_size,
        num_workers=num_workers,
        apply_aug=True,
        use_embeddings=use_embeddings,
    )
    optimizer, scheduler = get_optim(
        model,
        num_epochs=epochs,
        steps_per_epoch=len(train_dataloader) // grad_acc_steps,
    )

    (
        model,
        train_dataloader,
        val_dataloader,
        optimizer,
        scheduler,
    ) = accelerator.prepare(
        model,
        train_dataloader,
        val_dataloader,
        optimizer,
        scheduler,
    )

    try:
        accelerator.load_state(checkpoint_dir)
    except Exception as e:
        raise Exception(
            f"Failed to load checkpoint: {e}\n"
            "This could be due to a mismatch between the tokenizer used "
            "to build the pre-training and fine-tuning datasets"
        )
    logger.info(f"Loaded checkpoint at {checkpoint_dir}")
    logger.info("Starting train job")

    _train(
        epochs=epochs,
        accelerator=accelerator,
        model=model,
        train_dataloader=train_dataloader,
        val_dataloader=val_dataloader,
        use_embeddings=use_embeddings,
        optimizer=optimizer,
        scheduler=scheduler,
        steps_per_checkpoint=steps_per_checkpoint,
        resume_step=resume_step,
        resume_epoch=resume_epoch,
        project_dir=project_dir,
    )


def train(
    model_name: str,
    train_data_paths: List[str],
    val_data_path: str,
    use_embeddings: bool,
    num_workers: int,
    batch_size: int,
    grad_acc_steps: int,
    epochs: int,
    checkpoint_path: str | None = None,
    steps_per_checkpoint: int | None = None,
    project_dir: str = None,
):
    # Validate inputs
    assert 0 < num_workers <= 128, "Too many workers"
    assert epochs > 0, "Invalid number of epochs"
    assert batch_size > 0, "Invalid batch size"
    assert torch.cuda.is_available() is True, "CUDA not available"
    for train_data_path in train_data_paths:
        assert os.path.isdir(
            train_data_path
        ), f"No dir found at {train_data_path}"
    assert os.path.isdir(val_data_path), f"No dir found at {val_data_path}"

    tokenizer_name = get_tokenizer_name(train_data_paths, val_data_path)
    if tokenizer_name == "abs":
        tokenizer = AbsTokenizer()
    elif tokenizer_name == "rel":
        tokenizer = RelTokenizer()
    else:
        raise Exception("Invalid tokenizer name")

    accelerator = accelerate.Accelerator(
        project_dir=project_dir, gradient_accumulation_steps=grad_acc_steps
    )
    if accelerator.is_main_process:
        project_dir = setup_project_dir(project_dir)
        logger = setup_logger(project_dir)

    logger = get_logger(__name__)
    logger.info(f"Using project directory {project_dir}")
    logger.info(
        f"Using training config: "
        f"model_name={model_name}, "
        f"use_embeddings={use_embeddings}, "
        f"checkpoint_path={checkpoint_path}, "
        if checkpoint_path
        else ""
        f"epochs={epochs}, "
        f"batch_size={batch_size}, "
        f"grad_acc_steps={grad_acc_steps}, "
        f"num_workers={num_workers}"
    )

    if steps_per_checkpoint:
        logger.info(f"Creating checkpoints every {steps_per_checkpoint}")

    # Init model
    model_config = ModelConfig(**load_model_config(model_name))
    model_config.set_vocab_size(tokenizer.vocab_size)

    if use_embeddings is True:
        model = TransformerLM_CND(model_config)
    else:
        model = TransformerLM(model_config)

    model.compile()
    logger.info(f"Loaded model with config: {load_model_config(model_name)}")
    if checkpoint_path:
        try:
            model.load_state_dict(_load_weight(checkpoint_path))
        except RuntimeError as e:
            print(e)
            logger.info(
                f"Failed to load {model_name} into {model_name}, attempting with strict=False"
            )
            model.load_state_dict(_load_weight(checkpoint_path), strict=False)

        logger.info(f"Loaded finetune checkpoint located at: {checkpoint_path}")

    train_dataloader, val_dataloader = get_dataloaders(
        train_data_dirs=train_data_paths,
        val_data_dir=val_data_path,
        tokenizer=tokenizer,
        batch_size=batch_size,
        num_workers=num_workers,
        apply_aug=True,
        use_embeddings=use_embeddings,
    )

    assert (
        train_dataloader.dataset.config["max_seq_len"]
        == model_config.max_seq_len
    )
    assert (
        val_dataloader.dataset.config["max_seq_len"] == model_config.max_seq_len
    )

    optimizer, scheduler = get_optim(
        model,
        num_epochs=epochs,
        steps_per_epoch=len(train_dataloader) // grad_acc_steps,
    )

    (
        model,
        train_dataloader,
        val_dataloader,
        optimizer,
        scheduler,
    ) = accelerator.prepare(
        model,
        train_dataloader,
        val_dataloader,
        optimizer,
        scheduler,
    )

    logger.info(f"Starting {'finetune' if checkpoint_path else 'pretrain'} job")
    _train(
        epochs=epochs,
        accelerator=accelerator,
        model=model,
        train_dataloader=train_dataloader,
        val_dataloader=val_dataloader,
        use_embeddings=use_embeddings,
        optimizer=optimizer,
        scheduler=scheduler,
        steps_per_checkpoint=steps_per_checkpoint,
        project_dir=project_dir,
    )


def convert_cp_from_safetensors(checkpoint_path: str, save_path: str):
    d = load_file(checkpoint_path)
    key = list(d.keys())[0]
    gap = len(key.split(".")[0])
    d = {s[gap + 1 :]: v for s, v in d.items()}
    torch.save(d, save_path)


def convert_cp_from_accelerate(
    model_name: str, tokenizer_name: str, checkpoint_dir: str, save_path: str
):
    def _load_state_dict(_tokenizer: Tokenizer):
        model_config = ModelConfig(**load_model_config(model_name))
        model_config.set_vocab_size(_tokenizer.vocab_size)
        model = TransformerLM(model_config)
        model = accelerator.prepare(model)
        accelerator.load_state(checkpoint_dir)

        return model.state_dict()

    accelerator = accelerate.Accelerator()

    # Try both
    if tokenizer_name == "abs":
        state_dict = _load_state_dict(_tokenizer=AbsTokenizer())
    elif tokenizer_name == "rel":
        state_dict = _load_state_dict(_tokenizer=RelTokenizer())
    else:
        print("Invalid choice of tokenizer")

    torch.save(state_dict, save_path)


def parse_resume_args():
    argp = argparse.ArgumentParser(prog="python aria/train.py resume")
    argp.add_argument("model", help="name of model config file")
    argp.add_argument(
        "--train_data", nargs="+", help="path to train dir", required=True
    )
    argp.add_argument("--val_data", help="path to val dir", required=True)
    argp.add_argument(
        "--cp_dir", help="checkpoint dir", type=str, required=True
    )
    argp.add_argument(
        "--use_embeddings", help="prepend embeddings", action="store_true"
    )
    argp.add_argument("--r_step", help="resume step", type=int, required=True)
    argp.add_argument("--r_epoch", help="resume epoch", type=int, required=True)
    argp.add_argument("--epochs", help="train epochs", type=int, required=True)
    argp.add_argument("--bs", help="batch size", type=int, default=32)
    argp.add_argument(
        "--grad_acc_steps",
        help="gradient accumulation steps",
        type=int,
        default=1,
    )
    argp.add_argument("--workers", help="number workers", type=int, default=1)
    argp.add_argument("--pdir", help="project dir", type=str, required=False)
    argp.add_argument(
        "--spc", help="steps per checkpoint", type=int, required=False
    )

    return argp.parse_args(sys.argv[2:])


def parse_train_args():
    argp = argparse.ArgumentParser(prog="python aria/train.py train")
    argp.add_argument("model", help="name of model config file")
    argp.add_argument(
        "--train_data", nargs="+", help="path to train dir", required=True
    )
    argp.add_argument("--val_data", help="path to val dir", required=True)
    argp.add_argument(
        "--cp_path", help="path to checkpoint", required=False, default=None
    )
    argp.add_argument(
        "--use_embeddings", help="prepend embeddings", action="store_true"
    )
    argp.add_argument("--epochs", help="train epochs", type=int, required=True)
    argp.add_argument("--bs", help="batch size", type=int, default=32)
    argp.add_argument(
        "--grad_acc_steps",
        help="gradient accumulation steps",
        type=int,
        default=1,
    )
    argp.add_argument("--workers", help="number workers", type=int, default=1)
    argp.add_argument("--pdir", help="project dir", type=str, required=False)
    argp.add_argument(
        "--spc", help="steps per checkpoint", type=int, required=False
    )

    return argp.parse_args(sys.argv[2:])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        usage="python aria/train.py <command> [<args>]"
    )
    parser.add_argument(
        "mode", help="training function", choices=("train", "resume")
    )

    args = parser.parse_args(sys.argv[1:2])
    if not hasattr(args, "mode"):
        parser.print_help()
        print("Unrecognized command")
        exit(1)
    elif args.mode == "train":
        train_args = parse_train_args()
        train(
            model_name=train_args.model,
            train_data_paths=train_args.train_data,
            use_embeddings=train_args.use_embeddings,
            val_data_path=train_args.val_data,
            num_workers=train_args.workers,
            batch_size=train_args.bs,
            grad_acc_steps=train_args.grad_acc_steps,
            epochs=train_args.epochs,
            checkpoint_path=train_args.cp_path,
            steps_per_checkpoint=train_args.spc,
            project_dir=train_args.pdir,
        )
    elif args.mode == "resume":
        resume_args = parse_resume_args()
        resume_train(
            model_name=resume_args.model,
            train_data_paths=resume_args.train_data,
            val_data_path=resume_args.val_data,
            use_embeddings=resume_args.use_embeddings,
            num_workers=resume_args.workers,
            batch_size=resume_args.bs,
            grad_acc_steps=resume_args.grad_acc_steps,
            epochs=resume_args.epochs,
            checkpoint_dir=resume_args.cp_dir,
            resume_step=resume_args.r_step,
            resume_epoch=resume_args.r_epoch,
            steps_per_checkpoint=resume_args.spc,
            project_dir=resume_args.pdir,
        )
    else:
        print("Unrecognized command")
        parser.print_help()
        exit(1)
