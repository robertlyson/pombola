from __future__ import division

import dateutil
import json
import requests
import datetime
from urlparse import urlsplit
from collections import defaultdict

from .constants import API_REQUESTS_TIMEOUT

from django.core.cache import caches
from django.views.generic import TemplateView
from django.db.models import Q

from pombola.core.models import Position


class SAMpAttendanceView(TemplateView):
    template_name = "south_africa/mp_attendance.html"

    def calculate_abs_percenatge(self, num, total):
        """
        Return the truncated value of num, as a percentage of total.
        """
        return int("{:.0f}".format(num / total * 100))

    def download_attendance_data(self):
        attendance_url = next_url = 'https://api.pmg.org.za/committee-meeting-attendance/meetings-by-member/'

        cache = caches['pmg_api']
        results = cache.get(attendance_url)

        if results is None:
            results = []
            while next_url:
                resp = requests.get(next_url, timeout=API_REQUESTS_TIMEOUT)
                data = json.loads(resp.text)
                results.extend(data.get('results'))

                next_url = data.get('next')

            cache.set(attendance_url, results)

        # Results are returned from the API most recent first, which
        # is convenient for us.
        return results

    def just_path_of_pa_url(self, ma):
        return dict(
            ma, member=dict(
                ma['member'], pa_url=urlsplit(ma['member']['pa_url']).path))

    def build_minister_zero_attendance(self, minister):
        """
        Return a record in the same format as the PMG API data for ministers with
        no attendance records. These records should be populated with zero.
        """
        initials = "".join(name[0].upper() for name in minister.given_name.split())
        minister_name = "{}, {} {}".format(
            minister.family_name, minister.title, initials)

        return {'member': {
                    'name': minister_name,
                    'pa_url': minister.get_absolute_url(),
                    'party_name': minister.parties()[0].slug.upper()},
                'meetings': None}

    def filter_attendance(self, annual_attendance, ctx_party, ctx_pos):
        """
        Filter meeting attendance to only include items which match
        the party and position selected by the user.

        `ctx_party`, `ctx_pos` are the party and position parameters sent by the client.
        These values are directly from the GET parameters, and should be deemed unsafe.
        """

        attendance_records = annual_attendance['meetings_by_member']
        if ctx_party:
            attendance_records = [ma for ma in attendance_records if
                ma['member']['party_name'] == ctx_party]

        year = datetime.datetime.strptime(annual_attendance['end_date'], "%Y-%m-%d").year

        active_minister_positions = Position.objects \
            .title_slug_prefixes(['minister', 'deputy-minister']) \
            .active_during_year(year) \
            .select_related('person')

        ministers = defaultdict(list)
        for position in active_minister_positions:
            ministers[position.person.slug].append(position)

        minister_slugs = ministers.keys()

        minister_attendance = []
        mp_attendance = []

        for record in attendance_records:
            # Split records between MP and Minister attendance
            attendance_as_minister = []
            attendance_as_mp = []

            if record['member']['pa_url']:
                # We cannot determine a position if no `pa_url` was returned. Ignore these records.
                slug = record['member']['pa_url'].split('/')[-2]
                if slug in minister_slugs:
                    # This member was a minister during the year
                    positions = ministers[slug]
                    for meeting in record['meetings']:
                        # Check the member position at each meeting date.
                        minister_at_date = False
                        for position in positions:
                        # A member can have more than one active ministerial position in a year
                            if position.is_active_at_date(meeting['date']):
                                minister_at_date = True

                        if minister_at_date:
                            attendance_as_minister.append(meeting)
                        else:
                            attendance_as_mp.append(meeting)

                    # Member can be a Minister and an MP during the year
                    if attendance_as_minister:
                        minister_attendance.append({'member': record['member'], 'meetings': attendance_as_minister})
                        # Only remove if slug if minister attendance was added.
                        # If not, retain, as zero attendance zero attendance entry needs to be added.
                        minister_slugs.remove(slug)

                    if attendance_as_mp:
                        mp_attendance.append({'member': record['member'], 'meetings': attendance_as_mp})

                else:
                    # Member wasn't a minister during the year. All attendance as MP.
                    mp_attendance.append(record)

        if ctx_pos == 'ministers':
            # Ministers remaining in `minister_slugs` had no attendance records returned
            # Create a record for each.
            for slug in minister_slugs:
                minister = ministers[slug][0].person
                if ctx_party and minister.parties()[0].slug.upper() != ctx_party:
                    # Only include ministers belonging to the party selected.
                    continue
                else:
                    minister_attendance.append(self.build_minister_zero_attendance(minister))

            return minister_attendance

        return mp_attendance

    def get_attendance_summary(self, attendance):
        """Return the tallied attendance records"""
        attendance_summary = []
        for record in attendance:
            attendance_count = {}
            if not record['meetings']:
                # Ministers with no attendance records. Show zero attendance.
                attendance_summary.append({'member': record['member'], 'attendance': {'P': 0}})
            else:
                for meeting in record['meetings']:
                    attendance_count.setdefault(meeting['attendance'], 0)
                    attendance_count[meeting['attendance']] += 1

                attendance_summary.append({'member': record['member'], 'attendance': attendance_count})

        return attendance_summary

    def get_context_data(self, **kwargs):
        data = self.download_attendance_data()

        #  A:   Absent
        #  AP:  Absent with Apologies
        #  DE:  Departed Early
        #  L:   Arrived Late
        #  LDE: Arrived Late and Departed Early
        #  P:   Present

        present_codes = ['P', 'L', 'LDE', 'DE']
        arrive_late_codes = ['L', 'LDE']
        depart_early_codes = ['DE', 'LDE']

        # Page defaults
        context = {}
        context['year'] = str(
            dateutil.parser.parse(data[0]['end_date']).year)
        context['party'] = ''
        context['position'] = 'ministers'

        for key in ('year', 'party', 'position'):
            if key in self.request.GET:
                context[key] = self.request.GET[key]

        context['attendance_data'] = []
        context['years'] = []
        context['download_url'] = 'http://api.pmg.org.za/committee-meeting-attendance/data.xlsx'

        for annual_attendance in data:
            year = str(dateutil.parser.parse(annual_attendance['end_date']).year)
            context['years'].append(year)

            if year == context['year']:
                parties = set(ma['member']['party_name'] for
                    ma in annual_attendance['meetings_by_member'])
                parties.discard(None)
                context['parties'] = sorted(parties)

                attendance = self.filter_attendance(
                    annual_attendance, context['party'], context['position'])

                attendance_summary = self.get_attendance_summary(attendance)

                if context['position'] == 'mps':
                    aggregate_total = aggregate_present = 0

                    for summary in attendance_summary:
                        total = sum(v for v in summary['attendance'].itervalues())

                        present = sum(
                            v for k, v in summary['attendance'].iteritems()
                            if k in present_codes)

                        arrive_late = sum(
                            v for k, v in summary['attendance'].iteritems()
                            if k in arrive_late_codes)

                        depart_early = sum(
                            v for k, v in summary['attendance'].iteritems()
                            if k in depart_early_codes)

                        aggregate_total += total
                        aggregate_present += present

                        present_perc = self.calculate_abs_percenatge(present, total)
                        arrive_late_perc = self.calculate_abs_percenatge(arrive_late, total)
                        depart_early_perc = self.calculate_abs_percenatge(depart_early, total)

                        context['attendance_data'].append({
                            "name": summary['member']['name'],
                            "pa_url": summary['member']['pa_url'],
                            "party_name": summary['member']['party_name'],
                            "present": present_perc,
                            "absent": 100 - present_perc,
                            "arrive_late": arrive_late_perc,
                            "depart_early": depart_early_perc,
                            "total": total,
                        })

                    if aggregate_total == 0:
                        # To avoid a division by zero if there's no data...
                        aggregate_attendance = -1
                    else:
                        aggregate_attendance = self.calculate_abs_percenatge(aggregate_present, aggregate_total)
                    context['aggregate_attendance'] = aggregate_attendance

                else:
                    # Only show meetings attended for Ministers
                    # No aggregates are calculated
                    for summary in attendance_summary:
                        present = sum(
                            v for k, v in summary['attendance'].iteritems()
                            if k in present_codes)
                        context['attendance_data'].append({
                                "name": summary['member']['name'],
                                "pa_url": summary['member']['pa_url'],
                                "party_name": summary['member']['party_name'],
                                "present": present,
                        })

        return context
