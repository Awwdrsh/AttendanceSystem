"""Request contract of the upload endpoint (the TS zod schema of modules/files/routes.ts)."""
from rest_framework import serializers

UPLOAD_PURPOSES = ('employee_document', 'company_document', 'expense_receipt', 'travel_bill', 'helpdesk_attachment',
                   'training_certificate', 'profile_change', 'hiring_cv', 'avatar', 'tax_proof', 'grievance_attachment',
                   'task_attachment')


class UploadIn(serializers.Serializer):
    """JSON upload: {purpose, filename, dataUrl}."""
    purpose = serializers.ChoiceField(UPLOAD_PURPOSES)
    filename = serializers.CharField(min_length=1, max_length=200, trim_whitespace=False)
    dataUrl = serializers.CharField(min_length=10, trim_whitespace=False)


class StoredFileOut(serializers.Serializer):
    id = serializers.CharField()
    filename = serializers.CharField()
    mime = serializers.CharField()
    bytes = serializers.IntegerField()
    purpose = serializers.CharField()
