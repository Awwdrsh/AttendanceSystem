"""The standard-library pieces of the people slice: serializer contracts in the zod wording, Pillow photo resizing,
the csv module for imports, pyuca name order in the org chart."""
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from hajiri.core.errors import ValidationError
from hajiri.employees.service import parse_csv
from hajiri.people import service as ppl
from tests.web.factories import htmx, sign_in


def test_strict_bodies_refuse_unknown_keys(world):
    with pytest.raises(ValidationError) as e:
        ppl.create_branch(world.hr, {'code': 'PKR', 'nameEn': 'Pokhara', 'colour': 'red'})
    assert {'path': '', 'message': 'Unrecognized key: "colour"'} in e.value.issues
    with pytest.raises(ValidationError) as e:
        ppl.create_unit(world.hr, {'kind': 'division', 'code': '', 'nameEn': 'X'})
    msgs = {i['path']: i['message'] for i in e.value.issues}
    assert msgs['kind'] == 'Invalid option: expected one of "department"|"team"'
    assert msgs['code'] == 'Too small: expected string to have >=1 characters'


def test_profile_sections_keep_the_ts_messages(world):
    with pytest.raises(ValidationError) as e:
        ppl.save_section(world.hr, world.sita_id, 'ids', {'pan': '12345'})
    assert e.value.issues == [{'path': 'pan', 'message': 'PAN must be 9 digits'}]
    with pytest.raises(ValidationError) as e:
        ppl.save_section(world.hr, world.sita_id, 'education', {'items': [{'level': 'bachelor', 'institution': 'TU', 'startYear': 2020,
                                                                            'endYear': 2018}]})
    assert e.value.issues == [{'path': 'items.0', 'message': 'end year is before start year'}]
    with pytest.raises(ValidationError) as e:
        ppl.save_section(world.hr, world.sita_id, 'contact', {'personalPhone': 'call me'})
    assert e.value.issues == [{'path': 'personalPhone', 'message': 'phone number'}]


def test_a_large_photo_is_stored_as_a_320_px_jpeg(world):
    from PIL import Image

    from hajiri.files.store import read_file
    from django.test import Client
    buf = io.BytesIO()
    Image.new('RGB', (1200, 800), (200, 30, 30)).save(buf, 'PNG')
    c = Client()
    sign_in(c, world.sita)
    r = c.post('/_/people/me/avatar', {'file': SimpleUploadedFile('me.png', buf.getvalue(), content_type='image/png')}, **htmx())
    assert r.status_code == 200
    f = ppl.avatar_file(world.sita, world.sita_id)
    img = Image.open(io.BytesIO(read_file(str(world.company.id), f['id'])['data']))
    assert img.format == 'JPEG' and img.size == (320, 320)


def test_csv_import_reads_quotes_and_crlf():
    rows = parse_csv('emp_code,name_en\r\nE1,"Sharma, Sita"\r\n,\r\nE2,"Ram ""RB"" Bahadur"\n')
    assert rows == [['emp_code', 'name_en'], ['E1', 'Sharma, Sita'], ['E2', 'Ram "RB" Bahadur']]


def test_org_chart_roots_sort_like_the_browser():
    nodes = [{'id': 'b', 'name_en': 'ćaro', 'manager_id': None}, {'id': 'a', 'name_en': 'Zed', 'manager_id': None},
             {'id': 'c', 'name_en': 'bina', 'manager_id': None}]
    assert [r['name_en'] for r in ppl.build_tree(nodes)['roots']] == ['bina', 'ćaro', 'Zed']
