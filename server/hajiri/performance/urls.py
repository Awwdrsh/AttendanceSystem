from django.urls import path

from . import views

urlpatterns = [
    # Growth hub (pages/hubs.tsx GrowthHub)
    path('me/growth', views.my_goals, name='my_goals'),
    path('me/growth/reviews', views.my_reviews, name='my_reviews'),
    path('me/growth/one-on-ones', views.one_on_ones, name='one_on_ones'),
    path('me/growth/feedback', views.feedback_page, name='feedback'),
    # Performance hub
    path('performance', views.team_goals, name='team_goals'),
    path('performance/reviews', views.cycles, name='cycles'),
    # goals and KPIs
    path('_/performance/goals/new', views.goal_new, name='goal_new'),
    path('_/performance/goals/<uuid:id>', views.goal_detail, name='goal_detail'),
    path('_/performance/goals/<uuid:id>/checkin', views.goal_checkin, name='goal_checkin'),
    path('_/performance/goals/<uuid:id>/status', views.goal_status, name='goal_status'),
    path('_/performance/goals/<uuid:id>/cancel', views.goal_cancel, name='goal_cancel'),
    path('_/performance/kpis/apply', views.kpi_apply, name='kpi_apply'),
    path('_/performance/kpis/new', views.kpi_edit, name='kpi_new'),
    path('_/performance/kpis/<uuid:id>/edit', views.kpi_edit, name='kpi_edit'),
    # journal and feedback
    path('_/performance/journal', views.journal_add, name='journal_add'),
    path('_/performance/journal/<uuid:id>/delete', views.journal_delete, name='journal_delete'),
    path('_/performance/feedback/give', views.feedback_give, name='feedback_give'),
    path('_/performance/feedback/ask', views.feedback_ask, name='feedback_ask'),
    path('_/performance/feedback/<uuid:id>/answer', views.feedback_answer, name='feedback_answer'),
    path('_/performance/feedback/<uuid:id>/decline', views.feedback_decline, name='feedback_decline'),
    # 1-on-1s
    path('_/performance/one-on-ones/new', views.meeting_new, name='meeting_new'),
    path('_/performance/one-on-ones/<uuid:id>', views.meeting_detail, name='meeting_detail'),
    path('_/performance/one-on-ones/<uuid:id>/update', views.meeting_update, name='meeting_update'),
    path('_/performance/one-on-ones/<uuid:id>/items', views.meeting_item_add, name='meeting_item_add'),
    path('_/performance/one-on-one-items/<uuid:id>/toggle', views.meeting_item_toggle, name='meeting_item_toggle'),
    path('_/performance/one-on-one-items/<uuid:id>/delete', views.meeting_item_delete, name='meeting_item_delete'),
    # reviews and peers
    path('_/performance/reviews/<uuid:id>', views.review_detail, name='review_detail'),
    path('_/performance/reviews/<uuid:id>/save', views.review_save, name='review_save'),
    path('_/performance/reviews/<uuid:id>/calibrate', views.review_calibrate, name='review_calibrate'),
    path('_/performance/reviews/<uuid:id>/acknowledge', views.review_acknowledge, name='review_acknowledge'),
    path('_/performance/reviews/<uuid:id>/peers', views.review_peers, name='review_peers'),
    path('_/performance/reviews/<uuid:id>/print', views.review_print, name='review_print'),
    path('_/performance/peers/<uuid:id>/decide', views.peer_decide, name='peer_decide'),
    path('_/performance/peers/<uuid:id>/remove', views.peer_remove, name='peer_remove'),
    path('_/performance/peers/<uuid:id>/respond', views.peer_respond, name='peer_respond'),
    # cycles and competencies
    path('_/performance/cycles/new', views.cycle_edit, name='cycle_new'),
    path('_/performance/cycles/<uuid:id>/edit', views.cycle_edit, name='cycle_edit'),
    path('_/performance/cycles/<uuid:id>/delete', views.cycle_delete, name='cycle_delete'),
    path('_/performance/cycles/<uuid:id>/launch', views.cycle_launch, name='cycle_launch'),
    path('_/performance/cycles/<uuid:id>/advance', views.cycle_advance, name='cycle_advance'),
    path('_/performance/competencies/new', views.competency_edit, name='competency_new'),
    path('_/performance/competencies/<uuid:id>/edit', views.competency_edit, name='competency_edit'),
]
