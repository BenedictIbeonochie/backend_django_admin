from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .models import AdminInvite


class EmailLoginForm(AuthenticationForm):
    username = forms.EmailField(label="Email", widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class AdminInviteForm(forms.ModelForm):
    class Meta:
        model = AdminInvite
        fields = ["email", "full_name"]
        widgets = {
            "email": forms.EmailInput(attrs={"placeholder": "new.admin@example.com"}),
            "full_name": forms.TextInput(attrs={"placeholder": "Full name (optional)"}),
        }


class FlagResolveForm(forms.Form):
    resolution_notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=True)


class AcceptInviteForm(forms.Form):
    full_name = forms.CharField(max_length=200, required=False)
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, min_length=10)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput, min_length=10)

    def clean(self):
        data = super().clean()
        if data.get("password1") != data.get("password2"):
            raise forms.ValidationError("Passwords do not match.")
        return data
