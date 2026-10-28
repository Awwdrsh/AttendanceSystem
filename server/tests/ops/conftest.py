"""A company shaped like the one the demo suite was written for: four departments, line managers, an HR user who has
set a password, and fourteen active employees (the DEMO data set's Sita and Ram plus twelve more): the demo roster
(demo.roster.MAIN) put into the DEMO fixture company."""
import pytest

from tests.core.fixtures import *  # noqa: F401,F403

from demo.roster import MAIN, ensure_hr, staff_company

PEOPLE = MAIN.people
HR_EMAIL = 'laxmi@corevalley.test'
HR_PASSWORD = 'correct-horse-battery-staple'


@pytest.fixture
def suite_company(demo):
    """Company DEMO filled out for the demo suite. Returns {'company_id', 'employees': {code: id}, 'units', 'hr_user_id'}."""
    cid = demo['company_id']
    staffed = staff_company(cid, MAIN, branch_id=demo['branch_id'], shift_id=demo['shift_id'])
    hr, _ = ensure_hr(cid, MAIN, 'corevalley.test', password=HR_PASSWORD)
    assert hr.email == HR_EMAIL
    return {'company_id': cid, 'employees': staffed['employees'], 'units': staffed['units'], 'hr_user_id': str(hr.pk)}
