from django.urls import path

# JSON API (TS: /api/v1/relations/...), called by the React app with the cookie session (frontend/src/api/relations.js).
# Literal segments (committee, mine, stages, register) come before the <id> patterns that would also match them.
from . import api

_API = 'api/v1/relations/'
urlpatterns = [
    path(f'{_API}policy', api.PolicyView.as_view(), name='api-policy'),
    path(f'{_API}summary', api.SummaryView.as_view(), name='api-summary'),

    path(f'{_API}grievances/committee', api.CommitteeView.as_view(), name='api-committee'),
    path(f'{_API}grievances', api.GrievancesView.as_view(), name='api-grievances'),
    path(f'{_API}grievances/<str:id>', api.GrievanceView.as_view(), name='api-grievance'),
    path(f'{_API}grievances/<str:id>/files/<str:file_id>', api.GrievanceFileView.as_view(), name='api-grievance-file'),
    path(f'{_API}grievances/<str:id>/notes', api.GrievanceNotesView.as_view(), name='api-grievance-notes'),
    path(f'{_API}grievances/<str:id>/decision', api.GrievanceDecisionView.as_view(), name='api-grievance-decision'),
    path(f'{_API}grievances/<str:id>/appeal', api.GrievanceAppealView.as_view(), name='api-grievance-appeal'),
    path(f'{_API}grievances/<str:id>/appeal-decision', api.GrievanceAppealDecisionView.as_view(), name='api-grievance-appeal-decision'),
    path(f'{_API}grievances/<str:id>/withdraw', api.GrievanceWithdrawView.as_view(), name='api-grievance-withdraw'),
    path(f'{_API}grievances/<str:id>/close', api.GrievanceCloseView.as_view(), name='api-grievance-close'),

    path(f'{_API}disciplinary/stages', api.StagesView.as_view(), name='api-stages'),
    path(f'{_API}disciplinary/mine', api.MyCasesView.as_view(), name='api-cases-mine'),
    path(f'{_API}disciplinary', api.CasesView.as_view(), name='api-cases'),
    path(f'{_API}disciplinary/<str:id>', api.CaseView.as_view(), name='api-case'),
    path(f'{_API}disciplinary/<str:id>/show-cause', api.CaseShowCauseView.as_view(), name='api-case-show-cause'),
    path(f'{_API}disciplinary/<str:id>/respond', api.CaseRespondView.as_view(), name='api-case-respond'),
    path(f'{_API}disciplinary/<str:id>/decide', api.CaseDecideView.as_view(), name='api-case-decide'),
    path(f'{_API}disciplinary/<str:id>/letter', api.CaseLetterView.as_view(), name='api-case-letter'),
    path(f'{_API}disciplinary/<str:id>/withdraw', api.CaseWithdrawView.as_view(), name='api-case-withdraw'),

    path(f'{_API}milestones', api.MilestonesView.as_view(), name='api-milestones'),
    path(f'{_API}separating', api.SeparatingView.as_view(), name='api-separating'),
    path(f'{_API}employees/<str:id>/contract', api.ContractView.as_view(), name='api-contract'),

    path(f'{_API}assignments', api.AssignmentsView.as_view(), name='api-assignments'),
    path(f'{_API}assignments/<str:id>/end', api.AssignmentEndView.as_view(), name='api-assignment-end'),

    path(f'{_API}kye/campaigns', api.CampaignsView.as_view(), name='api-campaigns'),
    path(f'{_API}kye/campaigns/<str:id>', api.CampaignView.as_view(), name='api-campaign'),
    path(f'{_API}kye/campaigns/<str:id>/nudge', api.CampaignNudgeView.as_view(), name='api-campaign-nudge'),
    path(f'{_API}kye/campaigns/<str:id>/close', api.CampaignCloseView.as_view(), name='api-campaign-close'),
    path(f'{_API}kye/campaigns/<str:id>/respond', api.CampaignRespondView.as_view(), name='api-campaign-respond'),
    path(f'{_API}kye/mine', api.MyKyeView.as_view(), name='api-kye-mine'),
    path(f'{_API}completeness', api.CompletenessView.as_view(), name='api-completeness'),

    path(f'{_API}popups', api.PopupsView.as_view(), name='api-popups'),
    path(f'{_API}popups/<str:id>/dismiss', api.PopupDismissView.as_view(), name='api-popup-dismiss'),

    path(f'{_API}devices/mine', api.MyDevicesView.as_view(), name='api-devices-mine'),
    path(f'{_API}devices/mine/<str:id>', api.MyDeviceView.as_view(), name='api-device-mine'),
    path(f'{_API}devices/register', api.RegisterDeviceView.as_view(), name='api-device-register'),
    path(f'{_API}devices', api.DevicesView.as_view(), name='api-devices'),
    path(f'{_API}devices/<str:id>/<str:action>', api.DeviceDecisionView.as_view(), name='api-device-decision'),
]
