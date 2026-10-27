"""Forms for sign-in, password change and two-step sign-in (the zod schemas of modules/auth/routes.ts)."""
from django import forms


class LoginForm(forms.Form):
    email = forms.EmailField(max_length=200, widget=forms.EmailInput(attrs={
        'autocomplete': 'username', 'placeholder': 'you@corevalley.ai', 'autofocus': True}))
    password = forms.CharField(max_length=128, strip=False, widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}))


class TwoFactorLoginForm(forms.Form):
    challenge = forms.CharField(min_length=20, max_length=100, widget=forms.HiddenInput)
    code = forms.CharField(min_length=6, max_length=20)
    recovery = forms.BooleanField(required=False, widget=forms.HiddenInput)
    next = forms.CharField(required=False, max_length=500, widget=forms.HiddenInput)


class ChangePasswordForm(forms.Form):
    current_password = forms.CharField(label='Current password', max_length=128, strip=False,
                                       widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}))
    new_password = forms.CharField(label='New password', max_length=256, strip=False,
                                   help_text='At least 12 characters. No other rules.',
                                   widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))
    new_password_again = forms.CharField(label='Repeat new password', max_length=256, strip=False, required=False,
                                         widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

    def clean(self):
        data = super().clean()
        new, again = data.get('new_password'), data.get('new_password_again')
        # The web form asks twice (ChangePasswordPage.tsx); API clients may send the new password once.
        if new is not None and again not in (None, '') and new != again:
            raise forms.ValidationError('The two new passwords do not match. Type the same password in both fields.')
        return data


class CodeForm(forms.Form):
    code = forms.CharField(min_length=6, max_length=20)

    def clean_code(self):
        return self.cleaned_data['code'].strip()


class EnableForm(forms.Form):
    code = forms.CharField(min_length=6, max_length=10)

    def clean_code(self):
        return self.cleaned_data['code'].replace(' ', '').strip()


class DisableForm(forms.Form):
    password = forms.CharField(min_length=1, max_length=128, strip=False)
    code = forms.CharField(min_length=6, max_length=20)

    def clean_code(self):
        return self.cleaned_data['code'].strip()


def first_error(form: forms.Form) -> str:
    """One readable message for a form that failed validation ('field: message' like the API's issue list)."""
    for name, errs in form.errors.items():
        msg = errs[0]
        if name == '__all__':
            return msg
        label = form.fields[name].label or name.replace('_', ' ')
        return f'{label}: {msg}'
    return 'Please check the form.'
