from typing import Dict, Union

import lightning.pytorch as pl
import torch
import torch.nn.functional as F
from omegaconf import DictConfig
from overrides import overrides

from bert_squeeze.distillation.base_distiller import BaseDistiller
from bert_squeeze.utils.losses.distillation_losses import KLDivLoss
from bert_squeeze.utils.scorers import SummarizationScorer
from bert_squeeze.utils.types import DistillationLoss


class Seq2SeqDistiller(BaseDistiller):
    """
    Lightning module to distil a given teacher model into a given student one for seq2seq tasks

    Args:
        teacher (Union["pl.LightningModule", "torch.nn.Module"]):
            model to distil knowledge from
        student (Union["pl.LightningModule", "torch.nn.Module"]):
            model to use as a student
        training_config (DictConfig):
            configuration to use for training and to distil the teacher model
        teacher_checkpoint (str):
            path to checkpoints to load to the teacher model
    """

    def __init__(
        self,
        teacher: Union["pl.LightningModule", "torch.nn.Module"],
        student: Union["pl.LightningModule", "torch.nn.Module"],
        training_config: DictConfig,
        teacher_checkpoint: str = None,
        **kwargs,
    ):
        super().__init__(teacher, student, training_config, teacher_checkpoint, **kwargs)
        self._set_objectives()
        self._set_scorers()

    @overrides
    def loss(
        self,
        teacher_logits: torch.Tensor,
        student_logits: torch.Tensor,
        labels: torch.Tensor = None,
        ignore_index: int = -100,
        *args,
        **kwargs,
    ) -> DistillationLoss:
        """
        Method called for loss computation

        Args:
            teacher_logits (torch.Tensor):
                teacher's predictions
            student_logits (torch.Tensor):
                student's predictions
            labels (torch.Tensor):
                ground truth labels
            ignore_index (int):
                labels to ignore during loss computation
        Returns:

        """
        # Ignore soft labeled indices (where label is `ignore_index`)
        active_idx = labels != ignore_index
        if active_idx.sum().item() > 0:
            objective = self.loss_ce(student_logits[active_idx], labels[active_idx])
        else:
            objective = torch.tensor(0.0).to(labels.device)

        kd_loss = self.loss_distill(teacher_logits, student_logits)
        full_loss = (1 - self.params.alpha) * kd_loss + self.params.alpha * objective
        return DistillationLoss(kd_loss=kd_loss, objective=objective, full_loss=full_loss)

    @overrides
    def training_step(self, batch, _) -> torch.Tensor:
        """"""
        t_logits = self.get_teacher_logits(batch)
        s_logits = self.get_student_logits(batch)

        loss = self.loss(t_logits, s_logits, batch["s_labels"])

        self.s_scorer.add(s_logits.detach().cpu(), batch["s_labels"].cpu(), loss)
        if self.global_step > 0 and self.global_step % self.params.logging_steps == 0:
            logging_loss = {
                f"train/{key}": torch.stack(val).mean()
                for key, val in self.s_scorer.losses.items()
            }
            self.log_dict(logging_loss)

            self.log("train/acc", self.scorer.acc)
        return loss.full_loss

    @overrides
    def test_step(self, batch, _) -> Dict:
        """"""
        t_logits = self.get_teacher_logits(batch)
        s_logits = self.get_student_logits(batch)

        loss = self.loss(t_logits, s_logits, batch["s_labels"])
        self.s_test_scorer.add(
            s_logits.detach().cpu(), batch["labels"].detach().cpu(), loss
        )
        self.test_step_outputs.append(
            {"loss": loss.full_loss, "logits": s_logits.detach().cpu()}
        )
        return {"loss": loss.full_loss}

    @overrides
    def validation_step(self, batch, _) -> Dict:
        """"""
        t_logits = self.get_teacher_logits(batch)
        s_logits = self.get_student_logits(batch)

        loss = self.loss(t_logits, s_logits, batch["s_labels"])
        self.s_valid_scorer.add(
            s_logits.detach().cpu(), batch["s_labels"].detach().cpu(), loss
        )
        self.validation_step_outputs.append(
            {"loss": loss.full_loss, "logits": s_logits.detach().cpu()}
        )
        return {"loss": loss.full_loss}

    @overrides
    def on_validation_epoch_end(self) -> None:
        """"""
        if not self.trainer.sanity_checking:
            all_logits = torch.cat(
                [pred["logits"] for pred in self.validation_step_outputs]
            )
            all_probs = F.softmax(all_logits, dim=-1)
            labels_probs = [all_probs[:, i] for i in range(all_probs.shape[-1])]
            self.log_eval_report(labels_probs)

        self.s_valid_scorer.reset()

    @overrides
    def on_test_epoch_end(self) -> None:
        """"""
        all_logits = torch.cat([pred["logits"] for pred in self.test_step_outputs])
        all_probs = F.softmax(all_logits, dim=-1)
        labels_probs = [all_probs[:, i] for i in range(all_probs.shape[-1])]

        self.log_eval_report(labels_probs)
        self.s_test_scorer.reset()

    def _set_objectives(self) -> None:
        """"""
        distillation_loss = self.params.get("distillation_loss", "kl")

        self.loss_distill = {"mse": torch.nn.MSELoss(), "kl": KLDivLoss()}[
            distillation_loss
        ]

    def _set_scorers(self) -> None:
        """
        Method to set the scorers to use to evaluate the model.
        """
        self.s_scorer = SummarizationScorer(
            tokenizer_name=self.student.pretrained_model, do_mismatch=False
        )
        self.s_valid_scorer = SummarizationScorer(
            tokenizer_name=self.student.pretrained_model, do_mismatch=True
        )
        self.s_test_scorer = SummarizationScorer(
            tokenizer_name=self.student.pretrained_model, do_mismatch=True
        )
