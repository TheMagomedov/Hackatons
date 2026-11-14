"""Grounded marketing documents; proposals are not measured business results."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.shared.models import Contract

MarketingKind = Literal['strategy', 'competitors', 'video_script']


class MarketingSection(Contract):
    heading: str = Field(min_length=1, max_length=160)
    kind: Literal['observation', 'recommendation', 'hypothesis']
    text: str = Field(min_length=1, max_length=1800)
    source_ids: list[str] = Field(default_factory=list, max_length=20)


class VideoScene(Contract):
    seconds: int = Field(ge=2, le=20)
    visual: str = Field(min_length=1, max_length=600)
    voiceover: str = Field(default='', max_length=500)
    screen_text: str = Field(default='', max_length=120)


class MarketingReport(Contract):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1400)
    sections: list[MarketingSection] = Field(min_length=1, max_length=10)
    next_steps: list[str] = Field(min_length=1, max_length=8)
    measurement_plan: list[str] = Field(default_factory=list, max_length=6)
    warnings: list[str] = Field(default_factory=list, max_length=6)
    scenes: list[VideoScene] = Field(default_factory=list, max_length=6)

    @model_validator(mode='after')
    def bounded(self):
        if any(not text.strip() or len(text) > 700
               for text in [*self.next_steps, *self.measurement_plan, *self.warnings]):
            raise ValueError('Each action or warning must contain 1-700 characters')
        if sum(scene.seconds for scene in self.scenes) > 60:
            raise ValueError('Video script exceeds 60 seconds')
        if any(section.kind == 'observation' and not section.source_ids for section in self.sections):
            raise ValueError('Observations require sources')
        return self

    def texts(self) -> list[str]:
        return [self.title, self.summary, *self.next_steps, *self.measurement_plan, *self.warnings,
                *(s.heading + '\n' + s.text for s in self.sections),
                *(s.visual + '\n' + s.voiceover + '\n' + s.screen_text for s in self.scenes)]


class MarketingEvidence(Contract):
    id: str
    name: str
    url: str | None = None
    excerpt: str = Field(max_length=3000)
    captured_at: AwareDatetime


class MarketingDocument(Contract):
    kind: MarketingKind
    request: str
    profile_version: int
    created_at: AwareDatetime
    report: MarketingReport
    evidence: list[MarketingEvidence]
