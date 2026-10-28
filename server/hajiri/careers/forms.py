"""
The careers page's application form (CareersPage.tsx ApplyForm): reads the multipart post into the body the careers
service validates (the JSON API's shape without the data URL), and checks the CV before it is sent on. What was
typed is kept for showing the form again with the problem above the button.
"""
from __future__ import annotations

from django import forms

from hajiri.core.errors import BadRequestError
from hajiri.files.store import ParsedFile
from hajiri.hiring.forms import _number

CV_MAX = 4 * 1024 * 1024
NOT_PDF = 'Please attach your CV as a PDF.'
TOO_BIG = 'Your CV is larger than 4 MB. Please send a smaller PDF.'


class ApplicationForm(forms.Form):
    name = forms.CharField(required=False)
    email = forms.CharField(required=False)
    phone = forms.CharField(required=False)
    experienceYears = forms.CharField(required=False)
    currentEmployer = forms.CharField(required=False)
    coverNote = forms.CharField(required=False)
    consent = forms.CharField(required=False)
    website = forms.CharField(required=False, strip=False)
    cv = forms.FileField(required=False)

    def __init__(self, data, files, questions: list[dict]):
        super().__init__(data, files)
        self.questions = questions
        self.is_valid()

    def typed(self) -> dict:
        """What the person typed, to fill the form in again."""
        return {k: self.data.get(k, '') for k in ('name', 'email', 'phone', 'experienceYears', 'currentEmployer', 'coverNote')}

    def answers(self) -> dict:
        out = {}
        for q in self.questions:
            raw = self.data.get(f"answer_{q['id']}", '')
            if q['kind'] == 'number':
                v = _number(raw)
                if v is not None:
                    out[q['id']] = v
            elif raw.strip() != '':
                out[q['id']] = raw
        return out

    @property
    def consented(self) -> bool:
        return self.cleaned_data.get('consent') == '1'

    def cv_upload(self) -> tuple[str, ParsedFile]:
        """The CV's file name and bytes, or the page's message (missing, too big, not a PDF)."""
        cv = self.cleaned_data.get('cv')
        if not cv:
            raise BadRequestError(NOT_PDF)
        if cv.size > CV_MAX:
            raise BadRequestError(TOO_BIG)
        if cv.content_type != 'application/pdf':
            raise BadRequestError(NOT_PDF)
        return cv.name, ParsedFile('application/pdf', cv.read())

    def body(self, cv_filename: str) -> dict:
        d = self.cleaned_data
        out = {'name': d['name'], 'email': d['email'], 'phone': d['phone'], 'experienceYears': _number(d['experienceYears']),
               'currentEmployer': d['currentEmployer'] or None, 'coverNote': d['coverNote'] or None,
               'answers': self.answers(), 'cvFilename': cv_filename, 'consent': self.consented}
        if self.data.get('website'):
            out['website'] = self.data['website']
        return out
