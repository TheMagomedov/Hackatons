"""Explicit UTM attribution and declared leads; no inferred sales or reach."""

from typing import Literal

from pydantic import Field, model_validator

from app.shared.models import Contract


class CampaignInput(Contract):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000)
    source: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    medium: str = Field(default='social', pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    campaign: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    request_id: str = Field(min_length=1, max_length=100)


class LeadInput(Contract):
    title: str = Field(min_length=1, max_length=160)
    campaign_id: str | None = Field(default=None, min_length=1, max_length=100)
    stage: Literal['new', 'qualified', 'won', 'lost'] = 'new'
    amount_kopecks: int = Field(default=0, strict=True, ge=0, le=100_000_000_000)
    note: str = Field(default='', max_length=1000)
    request_id: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def won_amount(self):
        if self.amount_kopecks and self.stage != 'won':
            raise ValueError('Сумму можно указать только для закрытой сделки.')
        if not self.title.strip():
            raise ValueError('Укажите название обращения.')
        return self


class LeadEdit(Contract):
    revision: int = Field(ge=1, strict=True)
    stage: Literal['new', 'qualified', 'won', 'lost']
    amount_kopecks: int = Field(default=0, strict=True, ge=0, le=100_000_000_000)

    @model_validator(mode='after')
    def won_amount(self):
        if self.amount_kopecks and self.stage != 'won':
            raise ValueError('Сумму можно указать только для закрытой сделки.')
        return self
