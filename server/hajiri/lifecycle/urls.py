from django.urls import path

# JSON API for the React app and Bearer-token clients (TS: /api/v1/lifecycle/...). Ids are <str:...> so the route
# functions answer a malformed one with the TS 400 "Invalid UUID"; fixed paths come before the <id> ones.
from . import api_views as a

L = 'api/v1/lifecycle'
urlpatterns = [
    path(f'{L}/settings', a.SettingsView.as_view(), name='api_settings'),
    path(f'{L}/templates', a.TemplatesView.as_view(), name='api_templates'),
    path(f'{L}/templates/<str:id>', a.TemplateView.as_view(), name='api_template'),
    path(f'{L}/cases', a.CasesView.as_view(), name='api_cases'),
    path(f'{L}/cases/<str:id>', a.CaseView.as_view(), name='api_case'),
    path(f'{L}/cases/<str:id>/cancel', a.CaseCancelView.as_view(), name='api_case_cancel'),
    path(f'{L}/cases/<str:id>/tasks', a.CaseTasksView.as_view(), name='api_case_tasks'),
    path(f'{L}/tasks/<str:id>', a.TaskView.as_view(), name='api_task'),
    path(f'{L}/tasks/<str:id>/comments', a.TaskCommentsView.as_view(), name='api_task_comments'),
    path(f'{L}/my-tasks', a.MyTasksView.as_view(), name='api_my_tasks'),
    path(f'{L}/employees/<str:id>/summary', a.EmployeeSummaryView.as_view(), name='api_employee_summary'),
    path(f'{L}/probation', a.ProbationView.as_view(), name='api_probation'),
    path(f'{L}/probation/reviews', a.ProbationReviewsView.as_view(), name='api_probation_reviews'),
    path(f'{L}/probation/confirm', a.ProbationConfirmView.as_view(), name='api_probation_confirm'),
    path(f'{L}/exits', a.ExitsView.as_view(), name='api_exits'),
    path(f'{L}/exits/mine', a.MyExitsView.as_view(), name='api_my_exits'),
    path(f'{L}/exits/resign', a.ResignView.as_view(), name='api_resign'),
    path(f'{L}/exits/<str:id>', a.ExitView.as_view(), name='api_exit'),
    path(f'{L}/exits/<str:id>/withdraw', a.ExitWithdrawView.as_view(), name='api_exit_withdraw'),
    path(f'{L}/exits/<str:id>/cancel', a.ExitCancelView.as_view(), name='api_exit_cancel'),
    path(f'{L}/exits/<str:id>/complete', a.ExitCompleteView.as_view(), name='api_exit_complete'),
    path(f'{L}/exits/<str:id>/interview', a.ExitInterviewView.as_view(), name='api_exit_interview'),
    path(f'{L}/exit-questions', a.ExitQuestionsView.as_view(), name='api_exit_questions'),
    path(f'{L}/asset-types', a.AssetTypesView.as_view(), name='api_asset_types'),
    path(f'{L}/assets', a.AssetsView.as_view(), name='api_assets'),
    path(f'{L}/assets/mine', a.MyAssetsView.as_view(), name='api_my_assets'),
    path(f'{L}/assets/<str:id>', a.AssetView.as_view(), name='api_asset'),
    path(f'{L}/assets/<str:id>/history', a.AssetHistoryView.as_view(), name='api_asset_history'),
    path(f'{L}/assets/<str:id>/assign', a.AssetAssignView.as_view(), name='api_asset_assign'),
    path(f'{L}/assets/<str:id>/return', a.AssetReturnView.as_view(), name='api_asset_return'),
]
