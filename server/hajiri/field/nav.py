from hajiri.web.nav import NavItem, area

NAV: list[NavItem] = [
    NavItem(hub='attendance', label='Field visits', url_name='field:visits', order=70, roles=('manager', *area('attendance')),
            feature='field', icon='map-pin', palette_label='Field visits', keywords=('breaks', 'gps', 'client visits')),
    NavItem(hub='attendance', label='Kiosks', url_name='field:kiosks', order=90, module='attendance', feature='field',
            icon='device-tablet', palette_label='Kiosks', keywords=('tablet', 'pairing code', 'clock-in station')),
    NavItem(hub='settings', label='Locations', url_name='field:locations', order=50, roles=('hr',), icon='map-pin',
            keywords=('geofence', 'office location', 'breaks', 'field staff', 'kiosk pin')),
    NavItem(hub='my_attendance', label='My roster', url_name='field:my_roster', order=20, employee_only=True, icon='calendar-event',
            keywords=('shift swap', 'my shifts')),
    NavItem(hub='my_attendance', label='Visits & breaks', url_name='field:my_visits', order=30, employee_only=True, feature='field',
            icon='map-pin', keywords=('field visits', 'breaks')),
    NavItem(hub='my_attendance', label='Kiosk badge', url_name='field:my_kiosk', order=40, employee_only=True, feature='field',
            icon='qrcode', keywords=('kiosk pin', 'qr badge')),
]
