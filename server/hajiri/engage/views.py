"""
Engage hub pages (/engage, /engage/surveys, /engage/suggestions) and their HTMX fragments under /_/engage/...
(port of apps/web/src/pages/engage and components/engage). Rules live in services.py.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from functools import wraps

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import has_role
from hajiri.core.errors import ForbiddenError, HttpError, NotFoundError
from hajiri.web.decorators import login_required, require_feature, require_role
from hajiri.web.format import bs_month_label
from hajiri.web.htmx import close_modal, is_htmx, refresh, toast, trigger
from hajiri.web.middleware import error_message
from hajiri.web.nav import feature_on

from . import forms as F
from . import services as S
from .services import REACTIONS

REACTION_EMOJI = {'like': ('👍', 'Like'), 'love': ('❤️', 'Love'), 'clap': ('👏', 'Applause'), 'celebrate': ('🎉', 'Celebrate'),
                  'laugh': ('😄', 'Ha')}
CATEGORIES = [('general', 'General'), ('workplace', 'Workplace'), ('process', 'How we work'), ('benefits', 'Pay and benefits'),
              ('culture', 'Culture'), ('other', 'Other')]
CATEGORY_LABEL = dict(CATEGORIES)
KIND_LABEL = {'rating': 'Rating 1 to 5', 'enps': 'Recommend score 0 to 10', 'single': 'Pick one', 'multi': 'Pick any',
              'text': 'Written answer'}
REPEAT = {'none': '', 'weekly': 'Weekly', 'monthly': 'Monthly'}
SUGGESTION_STATUS = {'new': ('New', 'accent'), 'reviewed': ('Reviewed', 'info'), 'answered': ('Answered', 'ok'),
                     'archived': ('Archived', 'idle')}


# ---------------------------------------------------------------- helpers

def _ok(request, message: str | None, *refreshes: str, close: bool = False, status: int = 204) -> HttpResponse:
    resp = HttpResponse(status=status)
    if close:
        close_modal(resp)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message) if message else resp


def toast_errors(title: str):
    """toast.err(e, title): a refused request shows the error in a red toast with this title and changes nothing."""
    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            try:
                return view(request, *args, **kwargs)
            except HttpError as e:
                if not is_htmx(request):
                    raise
                r = HttpResponse(error_message(e), status=e.status, content_type='text/plain; charset=utf-8')
                r.headers['HX-Reswap'] = 'none'
                return trigger(r, 'hj:toast', {'message': error_message(e), 'tone': 'err', 'title': title})
        return wrapped
    return deco


def dom_id(*parts) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '-', '-'.join(str(p) for p in parts)).strip('-')


def _social(target_type: str, target_id: str, reactions: dict, mine: list, comments: int, compact: bool = False) -> dict:
    shown = [r for r in REACTIONS if not compact or reactions.get(r) or r in ('like', 'celebrate')]
    return {
        'target_type': target_type, 'target_id': target_id, 'dom': dom_id('s', target_type, target_id), 'compact': compact,
        'comments': comments,
        'chips': [{'key': r, 'emoji': REACTION_EMOJI[r][0], 'label': REACTION_EMOJI[r][1], 'n': reactions.get(r, 0),
                   'on': r in mine} for r in shown],
        'comment_label': (f"{comments} {'comment' if comments == 1 else 'comments'}" if comments
                          else 'Wish' if target_type == 'celebration' else 'Comment'),
    }


def _decorate(items: list[dict]) -> list[dict]:
    for i in items:
        i['social'] = _social(i['type'], i['id'], i['reactions'], i['mine'], i['comments'], compact=i['type'] == 'announcement')
        i['dom'] = dom_id(i['type'], i['id'])
    return items


def _flags(request) -> dict:
    user = request.hajiri_user
    return {'is_hr': has_role(user, 'hr'), 'can_announce': has_role(user, 'hr', 'manager'),
            'kudos_on': feature_on(request, 'kudos'), 'surveys_on': feature_on(request, 'surveys'),
            'suggestions_on': feature_on(request, 'suggestions'), 'is_employee': bool(user.employee_id)}


# ================================================================ feed

def _feed_ctx(request) -> dict:
    show = request.GET.get('show') or 'all'
    if show not in ('all', 'kudos', 'announcement'):
        show = 'all'
    # EngageFeedPage keeps every page loaded so far and applies the filter and pinned-first order to all of them, so
    # "Show older" asks for one more page and the whole list is drawn again (pages=N reloads pages 1..N).
    try:
        pages = min(max(int(request.GET.get('pages') or 1), 1), 50)
    except ValueError:
        pages = 1
    items: list[dict] = []
    q: dict = {'limit': '20'}
    nxt = None
    for _ in range(pages):
        data = S.feed(request.hajiri_user, q)
        items += data['items']
        nxt = data['nextBefore']
        if not nxt:
            break
        q = {'limit': '20', 'before': nxt}
    shown = [i for i in items if show == 'all' or i['type'] == show]
    pinned = [i for i in shown if i['type'] == 'announcement' and i['pinned']]
    rest = [i for i in shown if not (i['type'] == 'announcement' and i['pinned'])]
    return {'items': _decorate(pinned + rest), 'next': nxt, 'show': show, 'pages': pages, 'next_pages': pages + 1,
            **_flags(request)}


@require_GET
@login_required
def feed_page(request):
    ctx = _feed_ctx(request)
    if is_htmx(request) and request.headers.get('HX-Target') == 'engage-feed':
        return render(request, 'engage/partials/feed.html', ctx)
    ctx['filters'] = [('all', 'Everything'), ('kudos', 'Kudos'), ('announcement', 'Announcements')]
    return render(request, 'engage/feed.html', ctx)


@require_GET
@login_required
def feed_partial(request):
    return render(request, 'engage/partials/feed.html', _feed_ctx(request))


# ---------------------------------------------------------------- side panels

def _celebration_rows(user) -> list[dict]:
    from hajiri.web.format import fmt_date
    out = S.celebrations_for(user, {'days': '14'})
    for c in out:
        if c['kind'] == 'joined':
            c['when'] = 'Joined today' if c['daysAway'] == 0 else f"Joined {fmt_date(c['date'])}"
        else:
            c['when'] = 'Today' if c['daysAway'] == 0 else 'Tomorrow' if c['daysAway'] == 1 else c['bsLabel']
        c['what'] = ('Birthday' if c['kind'] == 'birthday' else
                     f"{c['years']} {'year' if c['years'] == 1 else 'years'} at the company" if c['kind'] == 'anniversary'
                     else 'New joiner')
        c['social'] = _social('celebration', c['targetId'], c['reactions'], c['mine'], c['comments'], compact=True)
        c['dom'] = dom_id('c', c['targetId'])
    return out


@require_GET
@login_required
def celebrations_panel(request):
    user = request.hajiri_user
    return render(request, 'engage/partials/celebrations.html',
                  {'celebrations': _celebration_rows(user), 'prefs': S.get_prefs(user) if user.employee_id else None})


@require_POST
@login_required
@toast_errors('Not saved')
def save_prefs(request):
    S.save_prefs(request.hajiri_user, F.PrefsForm(request.POST).prefs())
    return _ok(request, None, 'engage-celebrations')


@require_GET
@login_required
@require_feature('kudos')
def leaderboard_panel(request):
    d = S.leaderboard(request.hajiri_user)
    d['label'] = bs_month_label(d['bsYear'], d['bsMonth'])
    d['top_values'] = [v for v in d['values'] if v['count'] > 0][:4]
    return render(request, 'engage/partials/leaderboard.html', {'d': d})


@require_GET
@login_required
@require_feature('suggestions')
def suggestion_panel(request):
    return render(request, 'engage/partials/suggestion_box.html', {'answered': S.public_suggestions(request.hajiri_user)[:3]})


# ---------------------------------------------------------------- reactions and comments

@require_POST
@login_required
@toast_errors('Not saved')
def react(request):
    form = F.ReactForm(request.POST)
    body = form.body()
    r = S.react(request.hajiri_user, body)
    s = _social(body['targetType'], body['targetId'], r['reactions'], r['mine'], r['comments'],
                compact=form.cleaned_data['compact'])
    return render(request, 'engage/partials/reactions.html', {'s': s})


def _thread(request, target_type: str, target_id: str, compact: bool = False):
    user = request.hajiri_user
    comments = S.list_comments(user, {'targetType': target_type, 'targetId': target_id})
    s = _social(target_type, target_id, {}, [], len(comments), compact=compact)
    return render(request, 'engage/partials/comments.html', {'comments': comments, 's': s})


@require_http_methods(['GET', 'POST'])
@login_required
def comments(request):
    if request.method == 'GET':
        try:
            return _thread(request, request.GET.get('target_type', ''), request.GET.get('target_id', ''))
        except HttpError as e:
            if not is_htmx(request):
                raise
            r = HttpResponse(error_message(e), status=e.status)
            r.headers['HX-Reswap'] = 'none'
            return trigger(r, 'hj:toast', {'message': error_message(e), 'tone': 'err', 'title': 'Could not load comments'})
    return _add_comment(request)


@toast_errors('Not saved')
def _add_comment(request):
    c = S.add_comment(request.hajiri_user, F.CommentForm(request.POST).comment())
    return _thread(request, c['targetType'], c['targetId'])


@require_POST
@login_required
@toast_errors('Not saved')
def comment_delete(request, id):
    c = S.delete_comment(request.hajiri_user, str(id))
    return _thread(request, c['targetType'], c['targetId'])


# ---------------------------------------------------------------- kudos

@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('kudos')
@toast_errors('Kudos not sent')
def kudos_new(request):
    user = request.hajiri_user
    if request.method == 'POST':
        S.give_kudos(user, F.KudosForm(request.POST).kudos())
        return _ok(request, 'Kudos sent. They have been notified.', 'engage-feed', 'engage-leaderboard', close=True)
    people = [c for c in S.colleagues(user) if c['id'] != user.employee_id]
    values = [v for v in S.list_values(user)['values'] if v['active']]
    preset = [x for x in request.GET.getlist('to') if x]
    return render(request, 'engage/partials/kudos_form.html', {'people': people, 'values': values, 'preset': preset,
                                                               'names': [p['name'].lower() for p in people]})


@require_POST
@login_required
@require_feature('kudos')
@toast_errors('Not saved')
def kudos_delete(request, id):
    S.delete_kudos(request.hajiri_user, str(id))
    return _ok(request, 'Kudos removed.', 'engage-feed', 'engage-leaderboard')


@require_GET
@require_role('hr')
@require_feature('kudos')
def values_manager(request):
    d = S.list_values(request.hajiri_user)
    return render(request, 'engage/partials/values.html', {'values': d['values'], 'limit': d['kudosMonthlyLimit']})


@require_POST
@require_role('hr')
@require_feature('kudos')
@toast_errors('Not saved')
def value_new(request):
    S.create_value(request.hajiri_user, F.ValueForm(request.POST).value())
    return _values_fragment(request)


def _values_fragment(request):
    d = S.list_values(request.hajiri_user)
    return render(request, 'engage/partials/values.html', {'values': d['values'], 'limit': d['kudosMonthlyLimit']})


@require_POST
@require_role('hr')
@require_feature('kudos')
@toast_errors('Not saved')
def value_toggle(request, id):
    user = request.hajiri_user
    v = next((v for v in S.list_values(user)['values'] if v['id'] == str(id)), None)
    if not v:
        raise NotFoundError('value not found')
    S.update_value(user, str(id), {'active': not v['active']})
    return _values_fragment(request)


@require_POST
@require_role('hr')
@toast_errors('Not saved')
def save_settings(request):
    S.save_settings(request.hajiri_user, F.SettingsForm(request.POST).settings())
    return _ok(request, 'Saved.')


# ---------------------------------------------------------------- announcements

@require_http_methods(['GET', 'POST'])
@login_required
@toast_errors('Not posted')
def announcement_new(request):
    user = request.hajiri_user
    hr = has_role(user, 'hr')
    if request.method == 'POST':
        r = S.post_announcement(user, F.AnnouncementForm(request.POST).announcement(hr=hr))
        n = r['notified']
        return _ok(request, f"Posted. {n} {'person was' if n == 1 else 'people were'} notified.", 'engage-feed', close=True)
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('only HR and managers post announcements')
    today = S.npt_today()
    return render(request, 'engage/partials/announcement_form.html', {
        'is_hr': hr, 'opts': S.audience_options(user) if hr else {'branches': [], 'orgUnits': []}, 'today': today,
        'max_date': (date.fromisoformat(today) + timedelta(days=365)).isoformat()})


@require_POST
@login_required
@toast_errors('Not saved')
def announcement_pin(request, id):
    S.edit_announcement(request.hajiri_user, str(id), F.PinForm(request.POST).patch())
    return _ok(request, None, 'engage-feed')


@require_POST
@login_required
@toast_errors('Not saved')
def announcement_delete(request, id):
    S.delete_announcement(request.hajiri_user, str(id))
    return _ok(request, 'Announcement removed.', 'engage-feed')


@require_POST
@login_required
@toast_errors('Not saved')
def announcement_ack(request, id):
    S.ack_announcement(request.hajiri_user, str(id))
    return render(request, 'engage/partials/acked.html')


@require_GET
@login_required
def announcement_acks(request, id):
    d = S.announcement_acks(request.hajiri_user, str(id))
    d['pct'] = round(d['acked'] / d['audience'] * 100) if d['audience'] else 0
    return render(request, 'engage/partials/acks.html', {'d': d})


# ---------------------------------------------------------------- home card (Home calls this)

@require_GET
@login_required
def home_card(request):
    """The Company card on Home; empty when there is nothing to show."""
    d = S.home(request.hajiri_user)
    if not d['announcement'] and not d['celebrations'] and not d['survey']:
        return HttpResponse('')
    for c in d['celebrations']:
        c['label'] = (f"{c['name']}: birthday" if c['kind'] == 'birthday' else
                      f"{c['name']}: {c['years']} {'year' if c['years'] == 1 else 'years'}" if c['kind'] == 'anniversary'
                      else f"{c['name']}: joined")
    return render(request, 'engage/partials/home_card.html', {'d': d, 'more': max(0, len(d['celebrations']) - 4)})


# ================================================================ surveys

def _survey_status(s: dict, today: str) -> tuple[str, str]:
    if s['status'] == 'draft':
        return 'Draft', 'idle'
    if s['status'] == 'closed':
        return 'Closed', 'idle'
    if s['opensOn'] > today:
        from hajiri.web.format import fmt_date
        return f"Opens {fmt_date(s['opensOn'])}", 'info'
    return 'Open', 'accent'


def _surveys_ctx(request) -> dict:
    user = request.hajiri_user
    hr = has_role(user, 'hr')
    today = S.npt_today()
    manage = S.manage_surveys(user) if hr else []
    for s in manage:
        s['badge'] = _survey_status(s, today)
        s['meta'] = ('Poll' if s['kind'] == 'poll' else 'Survey') + (' · anonymous' if s['anonymous'] else '') + \
            (f" · {REPEAT[s['recurrence']]}" if REPEAT[s['recurrence']] else '')
    return {'is_hr': hr, 'open': S.open_surveys_for(user) if user.employee_id else [], 'manage': manage,
            'is_employee': bool(user.employee_id)}


@require_GET
@login_required
@require_feature('surveys')
def surveys_page(request):
    ctx = _surveys_ctx(request)
    if is_htmx(request) and request.headers.get('HX-Target') == 'engage-surveys':
        return render(request, 'engage/partials/surveys.html', ctx)
    return render(request, 'engage/surveys.html', ctx)


@require_GET
@login_required
@require_feature('surveys')
def surveys_partial(request):
    return render(request, 'engage/partials/surveys.html', _surveys_ctx(request))


BLANK_Q = {'kind': 'rating', 'prompt': '', 'options': [], 'required': True}
TEMPLATES = {
    'pulse': {'title': 'Monthly pulse', 'kind': 'survey', 'recurrence': 'monthly', 'questions': [
        {'kind': 'enps', 'prompt': 'How likely are you to recommend CoreValley as a place to work?', 'options': [], 'required': True},
        {'kind': 'rating', 'prompt': 'I have what I need to do my job well.', 'options': [], 'required': True},
        {'kind': 'rating', 'prompt': 'My manager supports my growth.', 'options': [], 'required': True},
        {'kind': 'text', 'prompt': 'What is one thing we could do better?', 'options': [], 'required': False},
    ]},
    'poll': {'title': 'Quick poll', 'kind': 'poll', 'recurrence': 'none',
             'questions': [{'kind': 'single', 'prompt': '', 'options': ['', ''], 'required': True}]},
    'blank': {'title': '', 'kind': 'survey', 'recurrence': 'none', 'questions': [BLANK_Q]},
}



def _builder(request, editing: dict | None):
    user = request.hajiri_user
    today = S.npt_today()
    if editing:
        form = {'kind': editing['survey']['kind'], 'title': editing['survey']['title'],
                'description': editing['survey']['description'] or '', 'anonymous': editing['survey']['anonymous'],
                'minGroup': editing['survey']['minGroup'], 'audienceKind': editing['survey']['audienceKind'],
                'audienceId': editing['survey']['audienceId'] or '', 'opensOn': editing['survey']['opensOn'],
                'closesOn': editing['survey']['closesOn'], 'recurrence': editing['survey']['recurrence'],
                'questions': [{'kind': q['kind'], 'prompt': q['prompt'], 'options': q['options'], 'required': q['required']}
                              for q in editing['questions']]}
    else:
        tpl = TEMPLATES['pulse']
        form = {'kind': tpl['kind'], 'title': tpl['title'], 'description': '', 'anonymous': True, 'minGroup': 5,
                'audienceKind': 'all', 'audienceId': '', 'opensOn': today,
                'closesOn': (date.fromisoformat(today) + timedelta(days=7)).isoformat(), 'recurrence': tpl['recurrence'],
                'questions': tpl['questions']}
    return render(request, 'engage/partials/survey_builder.html', {
        'form': form, 'editing': editing, 'templates': TEMPLATES, 'kinds': KIND_LABEL, 'opts': S.audience_options(user),
        'today': today, 'blank': BLANK_Q})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('surveys')
@toast_errors('Not saved')
def survey_new(request):
    if request.method == 'POST':
        S.create_survey(request.hajiri_user, F.SurveyForm(request.POST).survey())
        return _ok(request, 'Saved as a draft. Publish it when you are ready.', 'engage-surveys', close=True)
    return _builder(request, None)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('surveys')
@toast_errors('Not saved')
def survey_edit(request, id):
    if request.method == 'POST':
        S.update_survey(request.hajiri_user, str(id), F.SurveyForm(request.POST).survey())
        return _ok(request, 'Saved as a draft. Publish it when you are ready.', 'engage-surveys', close=True)
    return _builder(request, S.get_survey(request.hajiri_user, str(id)))


@require_POST
@require_role('hr')
@require_feature('surveys')
@toast_errors('Not saved')
def survey_publish(request, id):
    S.publish_survey(request.hajiri_user, str(id))
    return _ok(request, 'Published. People in the audience have been notified.', 'engage-surveys')


@require_POST
@require_role('hr')
@require_feature('surveys')
@toast_errors('Not saved')
def survey_close(request, id):
    S.close_survey(request.hajiri_user, str(id))
    return _ok(request, 'Closed.', 'engage-surveys')


@require_POST
@require_role('hr')
@require_feature('surveys')
@toast_errors('Not saved')
def survey_delete(request, id):
    S.delete_survey(request.hajiri_user, str(id))
    return _ok(request, 'Draft deleted.', 'engage-surveys')


@require_POST
@require_role('hr')
@require_feature('surveys')
@toast_errors('Not saved')
def survey_extend(request, id):
    S.update_survey(request.hajiri_user, str(id), F.ExtendForm(request.POST).patch())
    return _ok(request, 'Saved.', 'engage-surveys', close=True)



def _results_ctx(survey: dict, results: dict) -> dict:
    for q in results['questions']:
        if q.get('distribution'):
            q['bars'] = [{'label': f"Score {d['label']}" if q['kind'] in ('rating', 'enps') else d['label'], 'value': d['count']}
                         for d in q['distribution']]
    r = results
    return {'survey': survey, 'results': r, 'anonymous': survey['anonymous'],
            'pct': round(r['responses'] / r['audience'] * 100) if r['audience'] else 0}


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('surveys')
@toast_errors('Not sent')
def survey_answer(request, id):
    user = request.hajiri_user
    data = S.get_survey(user, str(id))
    if request.method == 'POST':
        r = S.respond(user, str(id), {'answers': F.answers_form(data['questions'], request.POST)})
        msg = 'Vote counted.' if data['survey']['kind'] == 'poll' else 'Thanks, your answers are in.'
        if r.get('results'):
            resp = render(request, 'engage/partials/results.html', _results_ctx(data['survey'], r['results']))
            refresh(resp, 'engage-surveys')
            return toast(request, resp, msg)
        return _ok(request, msg, 'engage-surveys', close=True)
    for q in data['questions']:
        q['scale'] = [str(n) for n in (range(1, 6) if q['kind'] == 'rating' else range(0, 11))] if q['kind'] in ('rating', 'enps') else []
    return render(request, 'engage/partials/survey_answer.html',
                  {'data': data, 'required_ids': [q['id'] for q in data['questions'] if q['required']]})


@require_GET
@login_required
@require_feature('surveys')
def survey_results(request, id):
    d = S.results(request.hajiri_user, str(id))
    return render(request, 'engage/partials/results.html', _results_ctx(d['survey'], d['results']))


@require_GET
@require_role('hr')
@require_feature('surveys')
def survey_results_csv(request, id):
    resp = HttpResponse(S.results_csv(request.hajiri_user, str(id)), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="survey-results.csv"'
    return resp


@require_GET
@require_role('hr')
@require_feature('surveys')
def survey_extend_form(request, id):
    d = S.get_survey(request.hajiri_user, str(id))
    return render(request, 'engage/partials/survey_extend.html', {'s': d['survey'], 'today': S.npt_today()})


# ================================================================ suggestions

@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('suggestions')
@toast_errors('Not sent')
def suggestion_new(request):
    if request.method == 'POST':
        S.suggest(request.hajiri_user, F.SuggestionForm(request.POST).suggestion())
        return _ok(request, 'Sent anonymously. Thank you.', close=True)
    return render(request, 'engage/partials/suggestion_form.html', {'categories': CATEGORIES})


def _suggestions_ctx(request) -> dict:
    status = request.GET.get('status') or 'open'
    if status not in ('open', 'answered', 'archived'):
        status = 'open'
    lst = S.list_suggestions(request.hajiri_user, {} if status == 'open' else {'status': status})
    if status == 'open':
        lst = [s for s in lst if s['status'] in ('new', 'reviewed')]
    for s in lst:
        s['badge'] = SUGGESTION_STATUS[s['status'] or 'new']
        s['category_label'] = CATEGORY_LABEL.get(s['category'], s['category'])
    return {'status': status, 'suggestions': lst,
            'tabs': [('open', 'To handle'), ('answered', 'Answered'), ('archived', 'Archived')]}


@require_GET
@require_role('hr')
@require_feature('suggestions')
def suggestions_page(request):
    ctx = _suggestions_ctx(request)
    if is_htmx(request) and request.headers.get('HX-Target') == 'engage-suggestions':
        return render(request, 'engage/partials/suggestions.html', ctx)
    return render(request, 'engage/suggestions.html', ctx)


@require_POST
@require_role('hr')
@require_feature('suggestions')
@toast_errors('Not saved')
def suggestion_update(request, id):
    body = F.SuggestionUpdateForm(request.POST).patch()
    S.update_suggestion(request.hajiri_user, str(id), body)
    msg = ('Marked as reviewed.' if body.get('status') == 'reviewed' else 'Archived.' if body.get('status') == 'archived'
           else 'Shared with everyone.' if body.get('isPublic') else 'No longer shared.')
    return _ok(request, msg, 'engage-suggestions')


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('suggestions')
@toast_errors('Not saved')
def suggestion_reply(request, id):
    user = request.hajiri_user
    s = S.get_suggestion(user, str(id))
    if request.method == 'POST':
        S.update_suggestion(user, str(id), F.ReplyForm(request.POST).patch())
        return _ok(request, 'Reply saved.', 'engage-suggestions', close=True)
    # SuggestionsPage: setShare(s.isPublic ?? true); is_public is never null, so the switch starts as it is stored.
    return render(request, 'engage/partials/suggestion_reply.html',
                  {'s': s, 'share': True if s['isPublic'] is None else s['isPublic']})

