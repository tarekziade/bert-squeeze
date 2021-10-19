import logging
import math

import matplotlib.pyplot as plt
import numpy as np
import pytorch_lightning as pl
import seaborn as sns
import torch
from omegaconf import ListConfig, DictConfig
from torch.nn import CrossEntropyLoss
from transformers import AutoModel, AutoConfig, AdamW, get_linear_schedule_with_warmup

from ..utils.losses import LabelSmoothingLoss
from ..utils.optimizers import BertAdam
from ..utils.scorer import Scorer


class BaseModule(pl.LightningModule):

    def __init__(self, training_config: DictConfig, model_config: str, num_labels: int, **kwargs):
        super(BaseModule, self).__init__()
        self.model_config = AutoConfig.from_pretrained(model_config, num_labels=num_labels)
        self.config = training_config

        self._set_scorers()
        self._build_model()
        self._set_objective()

    def _set_scorers(self):
        self.scorer = Scorer(self.model_config.num_labels)
        self.valid_scorer = Scorer(self.model_config.num_labels)
        self.test_scorer = Scorer(self.model_config.num_labels)

    def _build_model(self):
        self.encoder = AutoModel.from_config(self.model_config)
        self.classifier = torch.nn.Sequential(
            torch.nn.Dropout(self.model_config.hidden_dropout_prob),
            torch.nn.Linear(self.model_config.hidden_size, self.model_config.hidden_size),
            torch.nn.ReLU(),
            torch.nn.LayerNorm(self.model_config.hidden_size),
            torch.nn.Linear(self.model_config.hidden_size, self.model_config.num_labels)
        )

    def _set_objective(self):
        objective = self.config.get("objective", "ce")
        self.smoothing = self.config.get("smoothing", 0.0)
        self.class_weights = self.config.get("class_weights", [1.0] * self.model_config.num_labels)

        if objective == "lsl" and self.smoothing == 0.0:
            logging.warning("You are using label smoothing and the smoothing parameter"
                            "is set to 0.0.")
        elif objective == "weighted" and all([w == 1.0 for w in self.class_weights]):
            logging.warning("You are using a weighted CrossEntropy but the class"
                            "weights are all equal to 1.0.")
        self.objective = {
            "ce": CrossEntropyLoss(),
            "lsl": LabelSmoothingLoss(classes=self.model_config.num_labels,
                                      smoothing=self.smoothing),
            "weighted": CrossEntropyLoss(weight=torch.Tensor(self.class_weights)),
        }[objective]

    def loss(self, logits: torch.Tensor, labels: torch.Tensor, *args, **kwargs):
        return self.objective(logits.view(-1, self.model_config.num_labels), labels.view(-1))

    def configure_optimizers(self):
        optimizer_parameters = self._get_optimizer_parameters()
        if self.config.optimizer == "adamw":
            optimizer = AdamW(optimizer_parameters, lr=self.config.learning_rates[0],
                              eps=self.config.adam_eps)

            if self.config.lr_scheduler:
                num_training_steps = len(self.train_dataloader()) * self.config.num_epochs // \
                                     self.config.accumulation_steps

                warmup_steps = math.ceil(num_training_steps * self.config.warmup_ratio)
                scheduler = get_linear_schedule_with_warmup(optimizer,
                                                            num_warmup_steps=warmup_steps,
                                                            num_training_steps=num_training_steps)
                lr_scheduler = {"scheduler": scheduler, "name": "NeptuneLogger"}
                return [optimizer], [lr_scheduler]

        elif self.config.optimizer == "bertadam":
            num_training_steps = len(self.train_dataloader()) * self.config.num_epochs // \
                                 self.config.accumulation_steps
            optimizer = BertAdam(optimizer_parameters, lr=self.config.learning_rates[0],
                                 warmup=self.config.warmup_ratio, t_total=num_training_steps)

        elif self.config.optimizer == "adam":
            optimizer = torch.optim.Adam(self.parameters(), lr=self.config.learning_rates[0])
        else:
            raise ValueError(f"Optimizer '{self.config.optimizer}' not supported.")

        return [optimizer], []

    def _get_optimizer_parameters(self):
        no_decay = ['bias', 'gamma', 'beta', 'LayerNorm.weight']

        if self.config.discriminative_learning:
            if isinstance(self.config.learning_rates, ListConfig) and len(self.config.learning_rates) > 1:
                groups = [(f'layer.{i}.', self.config.learning_rates[i]) for i in range(12)]
            else:
                lr = self.config.learning_rates[0] if isinstance(self.config.learning_rates,
                                                                 ListConfig) else self.config.learning_rates
                groups = [(f'layer.{i}.', lr * pow(self.config.layer_lr_decay, 11 - i)) for i in range(12)]

            group_all = [f'layer.{i}.' for i in range(12)]
            no_decay_optimizer_parameters, decay_optimizer_parameters = [], []
            for g, l in groups:
                no_decay_optimizer_parameters.append(
                    {'params': [p for n, p in self.named_parameters() if
                                not any(nd in n for nd in no_decay) and any(nd in n for nd in [g])],
                     'weight_decay_rate': self.config.weight_decay, 'lr': l}
                )
                decay_optimizer_parameters.append(
                    {'params': [p for n, p in self.named_parameters() if
                                any(nd in n for nd in no_decay) and any(nd in n for nd in [g])],
                     'weight_decay_rate': 0.0, 'lr': l}
                )

            group_all_parameters = [
                {'params': [p for n, p in self.named_parameters() if
                            not any(nd in n for nd in no_decay) and not any(nd in n for nd in group_all)],
                 'weight_decay_rate': self.config.weight_decay},
                {'params': [p for n, p in self.named_parameters() if
                            any(nd in n for nd in no_decay) and not any(nd in n for nd in group_all)],
                 'weight_decay_rate': 0.0},
            ]
            optimizer_grouped_parameters = no_decay_optimizer_parameters + decay_optimizer_parameters \
                                           + group_all_parameters
        else:
            optimizer_grouped_parameters = [
                {'params': [p for n, p in self.named_parameters() if not any(nd in n for nd in no_decay)],
                 'weight_decay_rate': self.config.weight_decay},
                {'params': [p for n, p in self.named_parameters() if any(nd in n for nd in no_decay)],
                 'weight_decay_rate': 0.0}
            ]
        return optimizer_grouped_parameters

    def log_eval_report(self, probs: np.array):
        table = self.valid_scorer.get_table()
        self.logger.experiment["eval/report"].log(table)

        logging_loss = {key: torch.stack(val).mean() for key, val in self.valid_scorer.losses.items()}
        for key, value in logging_loss.items():
            self.logger.experiment[f"eval/loss_{key}"].log(value)

        eval_report = self.valid_scorer.to_dict()
        for key, value in eval_report.items():
            if not isinstance(value, list) and not isinstance(value, np.ndarray):
                self.logger.experiment["eval/{}".format(key)].log(value=value, step=self.global_step)

        for i in range(probs.shape[1]):
            fig = plt.figure(figsize=(15, 15))
            sns.distplot(probs[:, i], kde=False, bins=100)
            plt.title("Probability boxplot for label {}".format(i))
            self.logger.experiment["eval/dist_label_{}".format(i)].log(fig)
            plt.close("all")

    def forward(self, **kwargs):
        raise NotImplementedError()

    def training_step(self, batch, batch_idx, *args, **kwargs):
        raise NotImplementedError()

    def test_step(self, batch, batch_idx, *args, **kwargs):
        raise NotImplementedError()

    def validation_step(self, batch, batch_idx, *args, **kwargs):
        raise NotImplementedError()

    def freeze_encoder(self):
        """Freeze encoder layers"""
        for param in self.encoder.parameters():
            param.requires_grad = False

    def unfreeze_encoder(self):
        """Unfreeze encoder layers"""
        for param in self.encoder.parameters():
            param.requires_grad = True
