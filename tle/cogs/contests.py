import asyncio
import functools
import json
import logging
import time
import datetime as dt
from collections import defaultdict, namedtuple
import random

import discord
from discord.ext import commands
from matplotlib import pyplot as plt

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.util import cache_system2
from tle.util import codeforces_api as cf
from tle.util import db
from tle.util import discord_common
from tle.util import events
from tle.util import paginator
from tle.util import ranklist as rl
from tle.util import table
from tle.util import tasks
from tle.util import graph_common as gc
from tle.cogs.codeforces import CodeforcesCogError

_CONTESTS_PER_PAGE = 5
_CONTEST_PAGINATE_WAIT_TIME = 5 * 60
_STANDINGS_PER_PAGE = 15
_STANDINGS_PAGINATE_WAIT_TIME = 2 * 60
_FINISHED_CONTESTS_LIMIT = 5
_WATCHING_RATED_VC_WAIT_TIME = 5 * 60  # seconds
_RATED_VC_EXTRA_TIME = 10 * 60  # seconds
_MIN_RATED_CONTESTANTS_FOR_RATED_VC = 50
_TOP_PARTICIPANTS_SHOWN = 3

class ContestCogError(commands.CommandError):
    pass


def _contest_start_time_format(contest, tz):
    start = dt.datetime.fromtimestamp(contest.startTimeSeconds, tz)
    return f'{start.strftime("%d %b %y, %H:%M")} {tz}'


def _contest_duration_format(contest):
    duration_days, duration_hrs, duration_mins, _ = cf_common.time_format(contest.durationSeconds)
    duration = f'{duration_hrs}h {duration_mins}m'
    if duration_days > 0:
        duration = f'{duration_days}d ' + duration
    return duration


def _get_formatted_contest_desc(id_str, start, duration, url, max_duration_len):
    em = '\N{EN SPACE}'
    sq = '\N{WHITE SQUARE WITH UPPER RIGHT QUADRANT}'
    desc = (f'`{em}{id_str}{em}|'
            f'{em}{start}{em}|'
            f'{em}{duration.rjust(max_duration_len, em)}{em}|'
            f'{em}`[`link {sq}`]({url} "Link to contest page")')
    return desc


def _get_embed_fields_from_contests(contests):
    infos = [(contest.name, str(contest.id), _contest_start_time_format(contest, dt.timezone.utc),
              _contest_duration_format(contest), contest.register_url)
             for contest in contests]

    max_duration_len = max(len(duration) for _, _, _, duration, _ in infos)

    fields = []
    for name, id_str, start, duration, url in infos:
        value = _get_formatted_contest_desc(id_str, start, duration, url, max_duration_len)
        fields.append((name, value))
    return fields


async def _send_reminder_at(channel, role, contests, before_secs, send_time):
    delay = send_time - time.time()
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    values = cf_common.time_format(before_secs)

    def make(value, label):
        tmp = f'{value} {label}'
        return tmp if value == 1 else tmp + 's'

    labels = 'day hr min sec'.split()
    before_str = ' '.join(make(value, label) for label, value in zip(labels, values) if value > 0)
    desc = f'About to start in {before_str}'
    embed = discord_common.cf_color_embed(description=desc)
    for name, value in _get_embed_fields_from_contests(contests):
        embed.add_field(name=name, value=value)
    await channel.send(role.mention, embed=embed)


def _get_ongoing_vc_participants():
    """ Returns a set containing the `member_id`s of users who are registered in an ongoing vc.
    """
    ongoing_vc_ids = cf_common.user_db.get_ongoing_rated_vc_ids()
    ongoing_vc_participants = set()
    for vc_id in ongoing_vc_ids:
        vc_participants = set(cf_common.user_db.get_rated_vc_user_ids(vc_id))
        ongoing_vc_participants |= vc_participants
    return ongoing_vc_participants

class Contests(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        self.future_contests = None
        self.active_contests = None
        self.finished_contests = None
        self.start_time_map = defaultdict(list)
        self.task_map = defaultdict(list)

        self.member_converter = commands.MemberConverter()
        self.role_converter = commands.RoleConverter()

        self.logger = logging.getLogger(self.__class__.__name__)

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self):
        self._update_task.start()
        self._watch_rated_vcs_task.start()

    @tasks.task_spec(name='ContestCogUpdate',
                     waiter=tasks.Waiter.for_event(events.ContestListRefresh))
    async def _update_task(self, _):
        contest_cache = cf_common.cache2.contest_cache
        self.future_contests = contest_cache.get_contests_in_phase('BEFORE')
        self.active_contests = (contest_cache.get_contests_in_phase('CODING') +
                                contest_cache.get_contests_in_phase('PENDING_SYSTEM_TEST') +
                                contest_cache.get_contests_in_phase('SYSTEM_TEST'))
        self.finished_contests = contest_cache.get_contests_in_phase('FINISHED')

        # Future contests already sorted by start time.
        self.active_contests.sort(key=lambda contest: contest.startTimeSeconds)
        self.finished_contests.sort(key=lambda contest: contest.end_time, reverse=True)
        # Keep most recent _FINISHED_LIMIT
        self.finished_contests = self.finished_contests[:_FINISHED_CONTESTS_LIMIT]

        self.logger.info(f'Refreshed cache')
        self.start_time_map.clear()
        for contest in self.future_contests:
            if not cf_common.is_nonstandard_contest(contest):
                # Exclude non-standard contests from reminders.
                self.start_time_map[contest.startTimeSeconds].append(contest)
        self._reschedule_all_tasks()

    def _reschedule_all_tasks(self):
        for guild in self.bot.guilds:
            self._reschedule_tasks(guild.id)

    def _reschedule_tasks(self, guild_id):
        for task in self.task_map[guild_id]:
            task.cancel()
        self.task_map[guild_id].clear()
        self.logger.info(f'Tasks for guild {guild_id} cleared')
        if not self.start_time_map:
            return
        try:
            settings = cf_common.user_db.get_reminder_settings(guild_id)
        except db.DatabaseDisabledError:
            return
        if settings is None:
            return
        channel_id, role_id, before = settings
        channel_id, role_id, before = int(channel_id), int(role_id), json.loads(before)
        guild = self.bot.get_guild(guild_id)
        channel, role = guild.get_channel(channel_id), guild.get_role(role_id)
        for start_time, contests in self.start_time_map.items():
            for before_mins in before:
                before_secs = 60 * before_mins
                task = asyncio.create_task(
                    _send_reminder_at(channel, role, contests, before_secs, start_time - before_secs))
                self.task_map[guild_id].append(task)
        self.logger.info(f'{len(self.task_map[guild_id])} tasks scheduled for guild {guild_id}')

    @staticmethod
    def _make_contest_pages(contests, title):
        pages = []
        chunks = paginator.chunkify(contests, _CONTESTS_PER_PAGE)
        for chunk in chunks:
            embed = discord_common.cf_color_embed()
            for name, value in _get_embed_fields_from_contests(chunk):
                embed.add_field(name=name, value=value, inline=False)
            pages.append((title, embed))
        return pages

    async def _send_contest_list(self, ctx, contests, *, title, empty_msg):
        if contests is None:
            raise ContestCogError('Contest list not present')
        if len(contests) == 0:
            await ctx.send(embed=discord_common.embed_neutral(empty_msg))
            return
        pages = self._make_contest_pages(contests, title)
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=_CONTEST_PAGINATE_WAIT_TIME,
                           set_pagenum_footers=True)

    @commands.group(brief='Commands for listing contests',
                    invoke_without_command=True)
    async def clist(self, ctx):
        await ctx.send_help(ctx.command)

    @clist.command(brief='List future contests')
    async def future(self, ctx):
        """List future contests on Codeforces."""
        await self._send_contest_list(ctx, self.future_contests,
                                      title='Future contests on Codeforces',
                                      empty_msg='No future contests scheduled')

    @clist.command(brief='List active contests')
    async def active(self, ctx):
        """List active contests on Codeforces, namely those in coding phase, pending system
        test or in system test."""
        await self._send_contest_list(ctx, self.active_contests,
                                      title='Active contests on Codeforces',
                                      empty_msg='No contests currently active')

    @clist.command(brief='List recent finished contests')
    async def finished(self, ctx):
        """List recently concluded contests on Codeforces."""
        await self._send_contest_list(ctx, self.finished_contests,
                                      title='Recently finished contests on Codeforces',
                                      empty_msg='No finished contests found')

    @commands.group(brief='Commands for contest reminders',
                    invoke_without_command=True)
    async def remind(self, ctx):
        await ctx.send_help(ctx.command)

    @remind.command(brief='Set reminder settings')
    @commands.has_role(constants.TLE_ADMIN)
    async def here(self, ctx, role: discord.Role, *before: int):
        """Sets reminder channel to current channel, role to the given role, and reminder
        times to the given values in minutes."""
        if not role.mentionable:
            raise ContestCogError('The role for reminders must be mentionable')
        if not before or any(before_mins <= 0 for before_mins in before):
            raise ContestCogError('Please provide valid `before` values')
        before = sorted(before, reverse=True)
        cf_common.user_db.set_reminder_settings(ctx.guild.id, ctx.channel.id, role.id, json.dumps(before))
        await ctx.send(embed=discord_common.embed_success('Reminder settings saved successfully'))
        self._reschedule_tasks(ctx.guild.id)

    @remind.command(brief='Clear all reminder settings')
    @commands.has_role(constants.TLE_ADMIN)
    async def clear(self, ctx):
        cf_common.user_db.clear_reminder_settings(ctx.guild.id)
        await ctx.send(embed=discord_common.embed_success('Reminder settings cleared'))
        self._reschedule_tasks(ctx.guild.id)

    @remind.command(brief='Show reminder settings')
    async def settings(self, ctx):
        """Shows the role, channel and before time settings."""
        settings = cf_common.user_db.get_reminder_settings(ctx.guild.id)
        if settings is None:
            await ctx.send(embed=discord_common.embed_neutral('Reminder not set'))
            return
        channel_id, role_id, before = settings
        channel_id, role_id, before = int(channel_id), int(role_id), json.loads(before)
        channel, role = ctx.guild.get_channel(channel_id), ctx.guild.get_role(role_id)
        if channel is None:
            raise ContestCogError('The channel set for reminders is no longer available')
        if role is None:
            raise ContestCogError('The role set for reminders is no longer available')
        before_str = ', '.join(str(before_mins) for before_mins in before)
        embed = discord_common.embed_success('Current reminder settings')
        embed.add_field(name='Channel', value=channel.mention)
        embed.add_field(name='Role', value=role.mention)
        embed.add_field(name='Before', value=f'At {before_str} mins before contest')
        await ctx.send(embed=embed)

    @staticmethod
    def _get_remind_role(guild):
        settings = cf_common.user_db.get_reminder_settings(guild.id)
        if settings is None:
            raise ContestCogError('Reminders are not enabled.')
        _, role_id, _ = settings
        role = guild.get_role(int(role_id))
        if role is None:
            raise ContestCogError('The role set for reminders is no longer available.')
        return role

    @remind.command(brief='Subscribe to contest reminders')
    async def on(self, ctx):
        """Subscribes you to contest reminders. Use ';remind settings' to see the current
        settings.
        """
        role = self._get_remind_role(ctx.guild)
        if role in ctx.author.roles:
            embed = discord_common.embed_neutral('You are already subscribed to contest reminders')
        else:
            await ctx.author.add_roles(role, reason='User subscribed to contest reminders')
            embed = discord_common.embed_success('Successfully subscribed to contest reminders')
        await ctx.send(embed=embed)

    @remind.command(brief='Unsubscribe from contest reminders')
    async def off(self, ctx):
        """Unsubscribes you from contest reminders."""
        role = self._get_remind_role(ctx.guild)
        if role not in ctx.author.roles:
            embed = discord_common.embed_neutral('You are not subscribed to contest reminders')
        else:
            await ctx.author.remove_roles(role, reason='User unsubscribed from contest reminders')
            embed = discord_common.embed_success('Successfully unsubscribed from contest reminders')
        await ctx.send(embed=embed)

    @staticmethod
    def _get_cf_or_ioi_standings_table(problem_indices, handle_standings, deltas=None, *, mode):
        assert mode in ('cf', 'ioi')

        def maybe_int(value):
            return int(value) if mode == 'cf' else value

        header_style = '{:>} {:<}    {:^}  ' + '  '.join(['{:^}'] * len(problem_indices))
        body_style = '{:>} {:<}    {:>}  ' + '  '.join(['{:>}'] * len(problem_indices))
        header = ['#', 'Handle', '='] + problem_indices
        if deltas:
            header_style += '  {:^}'
            body_style += '  {:>}'
            header += ['\N{INCREMENT}']

        body = []
        for handle, standing in handle_standings:
            virtual = '#' if standing.party.participantType == 'VIRTUAL' else ''
            tokens = [standing.rank, handle + ':' + virtual, maybe_int(standing.points)]
            for problem_result in standing.problemResults:
                score = ''
                if problem_result.points:
                    score = str(maybe_int(problem_result.points))
                tokens.append(score)
            body.append(tokens)

        if deltas:
            for tokens, delta in zip(body, deltas):
                tokens.append('' if delta is None else f'{delta:+}')
        return header_style, body_style, header, body

    @staticmethod
    def _get_icpc_standings_table(problem_indices, handle_standings, deltas=None):
        header_style = '{:>} {:<}    {:^}  {:^}  ' + '  '.join(['{:^}'] * len(problem_indices))
        body_style = '{:>} {:<}    {:>}  {:>}  ' + '  '.join(['{:<}'] * len(problem_indices))
        header = ['#', 'Handle', '=', '-'] + problem_indices
        if deltas:
            header_style += '  {:^}'
            body_style += '  {:>}'
            header += ['\N{INCREMENT}']

        body = []
        for handle, standing in handle_standings:
            virtual = '#' if standing.party.participantType == 'VIRTUAL' else ''
            tokens = [standing.rank, handle + ':' + virtual, int(standing.points), int(standing.penalty)]
            for problem_result in standing.problemResults:
                score = '+' if problem_result.points else ''
                if problem_result.rejectedAttemptCount:
                    penalty = str(problem_result.rejectedAttemptCount)
                    if problem_result.points:
                        score += penalty
                    else:
                        score = '-' + penalty
                tokens.append(score)
            body.append(tokens)

        if deltas:
            for tokens, delta in zip(body, deltas):
                tokens.append('' if delta is None else f'{delta:+}')
        return header_style, body_style, header, body

    def _make_standings_pages(self, contest, problem_indices, handle_standings, deltas=None):
        pages = []
        handle_standings_chunks = paginator.chunkify(handle_standings, _STANDINGS_PER_PAGE)
        num_chunks = len(handle_standings_chunks)
        delta_chunks = paginator.chunkify(deltas, _STANDINGS_PER_PAGE) if deltas else [None] * num_chunks

        if contest.type == 'CF':
            get_table = functools.partial(self._get_cf_or_ioi_standings_table, mode='cf')
        elif contest.type == 'ICPC':
            get_table = self._get_icpc_standings_table
        elif contest.type == 'IOI':
            get_table = functools.partial(self._get_cf_or_ioi_standings_table, mode='ioi')
        else:
            assert False, f'Unexpected contest type {contest.type}'

        num_pages = 1
        for handle_standings_chunk, delta_chunk in zip(handle_standings_chunks, delta_chunks):
            header_style, body_style, header, body = get_table(problem_indices,
                                                               handle_standings_chunk,
                                                               delta_chunk)
            t = table.Table(table.Style(header=header_style, body=body_style))
            t += table.Header(*header)
            t += table.Line('\N{EM DASH}')
            for row in body:
                t += table.Data(*row)
            t += table.Line('\N{EM DASH}')
            page_num_footer = f' # Page: {num_pages} / {num_chunks}' if num_chunks > 1 else ''

            # We use yaml to get nice colors in the ranklist.
            content = f'```yaml\n{t}\n{page_num_footer}```'
            pages.append((content, None))
            num_pages += 1

        return pages

    @staticmethod
    def _make_contest_embed_for_ranklist(ranklist):
        contest = ranklist.contest
        assert contest.phase != 'BEFORE', f'Contest {contest.id} has not started.'
        embed = discord_common.cf_color_embed(title=contest.name, url=contest.url)
        phase = contest.phase.capitalize().replace('_', ' ')
        embed.add_field(name='Phase', value=phase)
        if ranklist.is_rated:
            embed.add_field(name='Deltas', value=ranklist.deltas_status)
        now = time.time()
        en = '\N{EN SPACE}'
        if contest.phase == 'CODING':
            elapsed = cf_common.pretty_time_format(now - contest.startTimeSeconds, shorten=True)
            remaining = cf_common.pretty_time_format(contest.end_time - now, shorten=True)
            msg = f'{elapsed} elapsed{en}|{en}{remaining} remaining'
            embed.add_field(name='Tick tock', value=msg, inline=False)
        else:
            start = _contest_start_time_format(contest, dt.timezone.utc)
            duration = _contest_duration_format(contest)
            since = cf_common.pretty_time_format(now - contest.end_time, only_most_significant=True)
            msg = f'{start}{en}|{en}{duration}{en}|{en}Ended {since} ago'
            embed.add_field(name='When', value=msg, inline=False)
        return embed

    @staticmethod
    def _make_contest_embed_for_vc_ranklist(ranklist, vc_start_time=None, vc_end_time=None):
        contest = ranklist.contest
        embed = discord_common.cf_color_embed(title=contest.name, url=contest.url)
        embed.set_author(name='VC Standings')
        now = time.time()
        if vc_start_time and vc_end_time:
            en = '\N{EN SPACE}'
            elapsed = cf_common.pretty_time_format(now - vc_start_time, shorten=True)
            remaining = cf_common.pretty_time_format(max(0,vc_end_time - now), shorten=True)
            msg = f'{elapsed} elapsed{en}|{en}{remaining} remaining'
            embed.add_field(name='Tick tock', value=msg, inline=False)
        return embed

    @staticmethod
    def _filter_rated_only_contestant_data(handles, ranklist):

        # Keep only rated contestants
        rated_contestants = [handle for handle in handles
                             if ranklist.standing_by_id.get_correct_handle(handle) in ranklist.delta_by_handle]

        # fix the actual ranks for cases like Edu rounds where unofficial ranks are also included in official standings
        current_rank = 0
        last_rank = 0
        last_score = (-1, -1)
        for contestant in ranklist.standings:
            handle = contestant.party.teamName or contestant.party.members[0].handle
            if handle in ranklist.delta_by_handle:
                current_score = (contestant.points, contestant.penalty)
                current_rank += 1
                dict = ranklist.standing_by_id[handle]._asdict()
                dict['rank'] = current_rank if current_score != last_score else last_rank
                last_rank = dict['rank']
                last_score = current_score
                ranklist.standing_by_id[handle] = cf.make_from_dict(cf.RanklistRow, dict)

        return rated_contestants, ranklist

    @commands.command(brief='Show ranklist for given handles and/or server members')
    async def ranklist(self, ctx, contest_id: int, *args: str):
        """
        Shows ranklist for the contest with given contest id. If handles contains
        '+server', all server members are included. No handles defaults to '+server'.
        Use '+official' for only showing rated participants for the round
        """
        (show_official,), handles = cf_common.filter_flags(args, ['+official'])
        handles = await cf_common.resolve_handles(ctx, self.member_converter, handles, maxcnt=None,
                                                  default_to_all_server=True)
        contest = cf_common.cache2.contest_cache.get_contest(contest_id)
        wait_msg = await ctx.channel.send('Generating ranklist, please wait...')
        ranklist = None
        try:
            ranklist = cf_common.cache2.ranklist_cache.get_ranklist(contest, show_official)
        except cache_system2.RanklistNotMonitored:
            if contest.phase == 'BEFORE':
                raise ContestCogError(f'Contest `{contest.id} | {contest.name}` has not started')
            ranklist = await cf_common.cache2.ranklist_cache.generate_ranklist(contest.id, fetch_changes=True,
                                                                               show_unofficial=not show_official)

        await wait_msg.delete()
        await ctx.channel.send(embed=self._make_contest_embed_for_ranklist(ranklist))
        await self._show_ranklist(channel=ctx.channel, contest_id=contest_id, handles=handles, ranklist=ranklist)

    async def _show_ranklist(self, channel, contest_id: int, handles: [str], ranklist, vc: bool = False, delete_after: float = None):
        contest = cf_common.cache2.contest_cache.get_contest(contest_id)
        if ranklist is None:
            raise ContestCogError('No ranklist to show')

        handle_standings = []
        for handle in handles:
            try:
                standing = ranklist.get_standing_row(handle)
            except rl.HandleNotPresentError:
                continue

            # Database has correct handle ignoring case, update to it
            handle = rl.Ranklist.get_ranklist_lookup_key(standing)
            if vc and standing.party.participantType != 'VIRTUAL':
                continue
            handle_standings.append((handle, standing))

        if not handle_standings:
            error = f'None of the handles are present in the ranklist of `{contest.name}`'
            if vc:
                await channel.send(embed=discord_common.embed_alert(error), delete_after=delete_after)
                return
            raise ContestCogError(error)

        handle_standings.sort(key=lambda data: data[1].rank)
        deltas = None
        if ranklist.is_rated:
            deltas = [ranklist.get_delta(handle) for handle, standing in handle_standings]

        problem_indices = [problem.index for problem in ranklist.problems]
        pages = self._make_standings_pages(contest, problem_indices, handle_standings, deltas)
        paginator.paginate(self.bot, channel, pages, wait_time=_STANDINGS_PAGINATE_WAIT_TIME, delete_after=delete_after)

    @commands.command(brief='Start a rated vc.', usage='<contest_id> <@user1 @user2 ...>')
    async def ratedvc(self, ctx, contest_id: int, *members: discord.Member):
        ratedvc_channel_id = cf_common.user_db.get_rated_vc_channel(ctx.guild.id)
        if not ratedvc_channel_id or ctx.channel.id != ratedvc_channel_id:
            raise ContestCogError('You must use this command in ratedvc channel.')
        if not members:
            raise ContestCogError('Missing members')
        contest = cf_common.cache2.contest_cache.get_contest(contest_id)
        try:
            (await cf.contest.ratingChanges(contest_id=contest_id))[_MIN_RATED_CONTESTANTS_FOR_RATED_VC - 1]
        except (cf.RatingChangesUnavailableError, IndexError):
            error = (f'`{contest.name}` was not rated for at least {_MIN_RATED_CONTESTANTS_FOR_RATED_VC} contestants'
                    ' or the ratings changes are not published yet.')
            raise ContestCogError(error)

        ongoing_vc_member_ids = _get_ongoing_vc_participants()
        this_vc_member_ids = {str(member.id) for member in members}
        intersection = this_vc_member_ids & ongoing_vc_member_ids
        if intersection:
            busy_members = ", ".join([ctx.guild.get_member(int(member_id)).mention for member_id in intersection])
            error = f'{busy_members} are registered in ongoing ratedvcs.'
            raise ContestCogError(error)

        handles = cf_common.members_to_handles(members, ctx.guild.id)
        visited_contests = await cf_common.get_visited_contests(handles)
        if contest_id in visited_contests:
            raise ContestCogError(f'Some of the handles: {", ".join(handles)} have submissions in the contest')
        start_time = time.time()
        finish_time = start_time + contest.durationSeconds + _RATED_VC_EXTRA_TIME
        cf_common.user_db.create_rated_vc(contest_id, start_time, finish_time, ctx.guild.id, [member.id for member in members])
        title = f'Starting {contest.name} for:'
        msg = "\n".join(f'[{discord.utils.escape_markdown(handle)}]({cf.PROFILE_BASE_URL}{handle})' for handle in handles)
        embed = discord_common.cf_color_embed(title=title, description=msg, url=contest.url)
        await ctx.send(embed=embed)
        embed = discord_common.embed_alert(f'You have {int(finish_time - start_time) // 60} minutes to complete the vc!')
        embed.set_footer(text='GL & HF')
        await ctx.send(embed=embed)

    @commands.command(
        brief="Show server members participation statistics", usage="[month] [handles]"
    )
    @commands.has_role(constants.TLE_ADMIN)
    async def serverstat(self, ctx, timed_month: str = "", *handles: str):
        """
        Show server member participation statistics for the given month. If handles contains
        '+server', all server members are included. No handles defaults to '+server'.
        Format month as 'mmm-YY', for example, 'may-20'. No month defaults to current month.
        """

        def monthToNum(shortMonth):
            return {
                "jan": 1,
                "feb": 2,
                "mar": 3,
                "apr": 4,
                "may": 5,
                "jun": 6,
                "jul": 7,
                "aug": 8,
                "sep": 9,
                "oct": 10,
                "nov": 11,
                "dec": 12,
            }[shortMonth]

        handles = set(handles)

        if not handles:
            handles.add("+server")
        if "+server" in handles:
            handles.remove("+server")
            guild_handles = [
                handle
                for discord_id, handle in cf_common.user_db.get_handles_for_guild(
                    ctx.guild.id
                )
            ]
            handles.update(guild_handles)
        handles = await cf_common.resolve_handles(
            ctx, self.member_converter, handles, maxcnt=None
        )

        if not timed_month:
            tp = dt.datetime.now()
            timed_month = tp.strftime("%b").lower() + "-" + tp.strftime("%y")

        try:
            month, year = timed_month.split("-")
            failed = not month or not year
        except ValueError:
            failed = True

        if failed:
            raise CodeforcesCogError(
                "Either do not supply month (to get current month) or supply mmm-YY. Example, 'may-20'"
            )

        year = int(year)
        mon_num = monthToNum(month)

        if year < 13 or mon_num < 0 or mon_num > 12:
            raise CodeforcesCogError("Invalid month or year")

        year += 2000
        start_timestamp = dt.datetime(year, mon_num, 1)
        if mon_num == 12:
            end_timestamp = dt.datetime(year + 1, 1, 1)
        else:
            end_timestamp = dt.datetime(year, mon_num + 1, 1)

        start_seconds = start_timestamp.timestamp()
        end_seconds = end_timestamp.timestamp()

        # we have to count number of contests users gave in the range from start to end timestamp

        finished_contests = cf_common.cache2.contest_cache.get_contests_in_phase(
            "FINISHED"
        )
        contests_usable = [
            contest
            for contest in finished_contests
            if start_seconds
            <= contest.startTimeSeconds + contest.durationSeconds
            < end_seconds and contest.id not in cf_common.cache_system2.CONTEST_BLACKLIST
        ]
        contests_usable = sorted(
            contests_usable, key=lambda x: x.startTimeSeconds + x.durationSeconds
        )

        async def get_standings(contest):
            try:
                ranklist = cf_common.cache2.ranklist_cache.get_ranklist(contest, True)
            except cache_system2.RanklistNotMonitored:
                ranklist = await cf_common.cache2.ranklist_cache.generate_ranklist(
                    contest.id, fetch_changes=True
                )
            return ranklist

        wait_msg = await ctx.send("Please wait...")

        """
        Figure out how caching works. Ideally, we should store ranklists and rating changes
        in cache and then fetch directly from cache.
        """
        contest_standings = [
            await get_standings(contest) for contest in contests_usable
        ]

        TIMES_PER_SECOND = 1
        PAUSE_DELAY = 1 / TIMES_PER_SECOND + 0.01

        class HandleContestData:
            rank_to_role = {role.name: role for role in ctx.guild.roles}

            def __init__(self, handle):
                self.handle = handle
                # includes official, unofficial
                self.contest_count = 0
                self.rated_count = 0
                # div2, div1
                self.rating_inc = 0
                self.average_rating = 0
                self.problems_solved = 0

                self.last_rating = 0
                self.peak_rating = 0
                self.initial_rating = -1

            def update_with_ranklist(self, ranklist):
                if self.handle not in ranklist.standing_by_id:
                    return

                self.contest_count += 1

                if (
                    ranklist.changes_by_handle
                    and self.handle in ranklist.changes_by_handle
                ):
                    change = ranklist.changes_by_handle[self.handle]
                    self.rated_count += 1
                    old, new = change.oldRating, change.newRating
                    self.average_rating += new
                    delta = new - old
                    if delta > 0:
                        self.rating_inc += delta
                    self.last_rating = new
                    self.peak_rating = max(self.peak_rating, new)
                    if self.initial_rating == -1:
                        self.initial_rating = old

            async def count_problems_solved(self):
                submissions = await cf.user.status(handle=self.handle)
                self.problems_solved = len(
                    set(
                        [
                            f"{sub.contestId}{sub.problem}"
                            for sub in submissions
                            if sub.verdict == "OK"
                            and start_seconds <= sub.creationTimeSeconds < end_seconds
                        ]
                    )
                )

            def rating_to_displayable_rank(self, rating):
                rank = cf.rating2rank(rating).title
                role = self.rank_to_role.get(rank)
                return role.mention if role else rank

            def get_rank_update(self):
                if self.initial_rating == -1:
                    return None
                init_rank = self.rating_to_displayable_rank(self.initial_rating)
                final_rank = self.rating_to_displayable_rank(self.peak_rating)
                if init_rank != final_rank and self.initial_rating < self.peak_rating:
                    return init_rank, final_rank

                return None

            def finalize(self):
                if self.rated_count != 0:
                    self.average_rating /= self.rated_count
                self.max_rating = 0
                # apparently one of these is None :/
                if self.initial_rating:
                    self.max_rating = max(self.max_rating, self.initial_rating)
                if self.peak_rating:
                    self.max_rating = max(self.max_rating, self.peak_rating)

            def __str__(self):
                return f"{self.handle}: {self.contest_count} {self.rated_count} {self.rating_inc} {self.average_rating} {self.problems_solved} ({self.initial_rating}, {self.last_rating}, {self.peak_rating})"

        handle_contest_data = []

        no_contest_handles = []

        for handle in handles:
            obj = HandleContestData(handle)
            for ranklist in contest_standings:
                obj.update_with_ranklist(ranklist)
            if obj.initial_rating == -1:
                no_contest_handles.append(obj.handle)
            handle_contest_data.append(obj)

        time.sleep(PAUSE_DELAY)
        for obj in handle_contest_data:
            await obj.count_problems_solved()
            time.sleep(PAUSE_DELAY)

        # this will handle chunk creation etc.
        information = await cf.user.info(handles=no_contest_handles)
        for info in information:
            for obj in handle_contest_data:
                if obj.handle == info.handle:
                    assert obj.initial_rating == -1
                    obj.peak_rating = obj.initial_rating = info.rating

        for obj in handle_contest_data:
            obj.finalize()

        for data in handle_contest_data[:30]:
            self.logger.info(str(data))

        div_thresholds = {1: [2100, 3000], 2: [1600, 2100], 3: [0, 1600]}

        def division_based_statistics(div_num):
            t_low, t_high = div_thresholds[div_num]
            # minimum number of rated contests required in given month
            count_requirement = {1: 1, 2: 2, 3: 3}
            usable_data = list(
                filter(
                    lambda data: t_low
                    <= data.max_rating
                    < t_high,
                    handle_contest_data,
                )
            )

            def sort_helper(sort_lambda):
                return sorted(
                    usable_data,
                    key=sort_lambda,
                    reverse=True,
                )[:_TOP_PARTICIPANTS_SHOWN]

            handle_contest_count = sort_helper(
                lambda x: (x.rated_count, x.average_rating)
            )
            highest_avg_rating = sort_helper(lambda x: x.average_rating)
            highest_rating_inc = sort_helper(lambda x: x.rating_inc)
            most_problems_solved = sort_helper(lambda x: x.problems_solved)

            handle_contest_count = [
                (x.handle, x.rated_count)
                for x in handle_contest_count
                if x.rated_count >= 1
            ]
            highest_avg_rating = [
                (x.handle, x.average_rating) for x in highest_avg_rating
            ]
            highest_rating_inc = [(x.handle, x.rating_inc) for x in highest_rating_inc]

            most_problems_solved = [
                (x.handle, x.problems_solved) for x in most_problems_solved
            ]

            return (
                handle_contest_count,
                highest_avg_rating,
                highest_rating_inc,
                most_problems_solved,
            )

        if wait_msg:
            try:
                await wait_msg.delete()
            except:
                pass

        user_id_handle_pairs = cf_common.user_db.get_handles_for_guild(ctx.guild.id)
        member_handle_pairs = [
            (ctx.guild.get_member(int(user_id)), handle)
            for user_id, handle in user_id_handle_pairs
        ]
        handle_to_member = {
            handle: member
            for member, handle in member_handle_pairs
            if member is not None
        }

        display_month = start_timestamp.strftime("%B") + " " + str(year)

        for div_num in range(1, 4):
            embed_color_seed = random.randrange(0, 100)
            embed = discord.Embed(
                title=f"Server stats for {display_month} (Div. {div_num})",
                color=embed_color_seed,
            )

            embed.set_author(name="Codeforces")
            embed.set_thumbnail(
                url="https://storage.googleapis.com/kaggle-datasets-images/742290/1285655/87aed221116e8abb4e01e98b15fd5b75/dataset-card.png?t=2020-06-28-00-49-57"
            )

            def make_embed(embed_title, data_list, residual=""):
                descriptions = []

                if residual == "":
                    for handle in data_list:
                        member = handle_to_member[handle]
                        mention_str = f"{member.mention} [{handle}]({cf.PROFILE_BASE_URL}{handle})"
                        descriptions.append(mention_str)
                else:
                    for handle, count in data_list:
                        cnt = int(count)
                        if cnt == 0:
                            break
                        member = handle_to_member[handle]
                        mention_str = f"{member.mention} [{handle}]({cf.PROFILE_BASE_URL}{handle}): {cnt} {residual}"
                        descriptions.append(mention_str)

                if descriptions:
                    value = "\n".join(descriptions)
                    embed.add_field(name=embed_title, value=value, inline=False)

            (
                most_contests,
                highest_avg_rating,
                highest_rating_inc,
                most_problems_solved,
            ) = division_based_statistics(div_num)

            mc_title = "Most contests given"
            make_embed(mc_title, most_contests, "contests")

            ha_title = "Highest average rating"
            make_embed(ha_title, highest_avg_rating, "avg rating")

            hr_title = "Highest rating increases"
            make_embed(
                hr_title,
                highest_rating_inc,
                "points",
            )

            mp_title = "Most problems solved"
            make_embed(
                mp_title,
                most_problems_solved,
                "problems",
            )

            await ctx.channel.send(embed=embed)

        # only expert or above rank updates
        rank_updates = list(
            filter(
                lambda x: x.max_rating >= 1600 and x.get_rank_update(),
                handle_contest_data,
            )
        )
        rank_updates = sorted(rank_updates, key=lambda x: x.max_rating, reverse=True)
        rank_changes_str = []
        for update in rank_updates:
            change_handle = update.handle
            old_role, new_role = update.get_rank_update()
            member = handle_to_member[change_handle]
            rank_change_str = (
                f"{member.mention} [{change_handle}]({cf.PROFILE_BASE_URL}{change_handle}): {old_role} "
                f"\N{LONG RIGHTWARDS ARROW} {new_role}"
            )
            rank_changes_str.append(rank_change_str)

        if rank_changes_str:
            embed_color_seed = random.randrange(0, 100)
            embed = discord.Embed(
                title=f"Rank upgrades of server members in {display_month}",
                color=embed_color_seed,
                description="\n".join(rank_changes_str),
            )

            embed.set_author(name="Codeforces")
            embed.set_thumbnail(
                url="https://storage.googleapis.com/kaggle-datasets-images/742290/1285655/87aed221116e8abb4e01e98b15fd5b75/dataset-card.png?t=2020-06-28-00-49-57"
            )

            await ctx.channel.send(embed=embed)

    @staticmethod
    def _make_vc_rating_changes_embed(guild, contest_id, change_by_handle):
        """Make an embed containing a list of rank changes and rating changes for ratedvc participants.
        """
        contest = cf_common.cache2.contest_cache.get_contest(contest_id)
        user_id_handle_pairs = cf_common.user_db.get_handles_for_guild(guild.id)
        member_handle_pairs = [(guild.get_member(int(user_id)), handle)
                               for user_id, handle in user_id_handle_pairs]
        member_change_pairs = [(member, change_by_handle[handle])
                               for member, handle in member_handle_pairs
                               if member is not None and handle in change_by_handle]

        member_change_pairs.sort(key=lambda pair: pair[1].newRating, reverse=True)
        rank_to_role = {role.name: role for role in guild.roles}

        def rating_to_displayable_rank(rating):
            rank = cf.rating2rank(rating).title
            role = rank_to_role.get(rank)
            return role.mention if role else rank

        rank_changes_str = []
        for member, change in member_change_pairs:
            if len(cf_common.user_db.get_vc_rating_history(member.id)) == 1:
                # If this is the user's first rated contest.
                old_role = 'Unrated'
            else:
                old_role = rating_to_displayable_rank(change.oldRating)
            new_role = rating_to_displayable_rank(change.newRating)
            if new_role != old_role:
                rank_change_str = (f'{member.mention} [{discord.utils.escape_markdown(change.handle)}]({cf.PROFILE_BASE_URL}{change.handle}): {old_role} '
                                   f'\N{LONG RIGHTWARDS ARROW} {new_role}')
                rank_changes_str.append(rank_change_str)

        member_change_pairs.sort(key=lambda pair: pair[1].newRating - pair[1].oldRating,
                                 reverse=True)
        rating_changes_str = []
        for member, change in member_change_pairs:
            delta = change.newRating - change.oldRating
            rating_change_str = (f'{member.mention} [{discord.utils.escape_markdown(change.handle)}]({cf.PROFILE_BASE_URL}{change.handle}): {change.oldRating} '
                            f'\N{HORIZONTAL BAR} **{delta:+}** \N{LONG RIGHTWARDS ARROW} '
                            f'{change.newRating}')
            rating_changes_str.append(rating_change_str)

        desc = '\n'.join(rank_changes_str) or 'No rank changes'
        embed = discord_common.cf_color_embed(title=contest.name, url=contest.url, description=desc)
        embed.set_author(name='VC Results')
        embed.add_field(name='Rating Changes',
                        value='\n'.join(rating_changes_str) or 'No rating changes',
                        inline=False)
        return embed

    async def _watch_rated_vc(self, vc_id: int):
        vc = cf_common.user_db.get_rated_vc(vc_id)
        channel_id = cf_common.user_db.get_rated_vc_channel(vc.guild_id)
        if channel_id is None:
            raise ContestCogError('No Rated VC channel')
        channel = self.bot.get_channel(int(channel_id))
        member_ids = cf_common.user_db.get_rated_vc_user_ids(vc_id)
        handles = [cf_common.user_db.get_handle(member_id, channel.guild.id) for member_id in member_ids]
        handle_to_member_id = {handle : member_id for handle, member_id in zip(handles, member_ids)}
        now = time.time()
        ranklist = await cf_common.cache2.ranklist_cache.generate_vc_ranklist(vc.contest_id, handle_to_member_id)

        async def has_running_subs(handle):
            return [sub for sub in await cf.user.status(handle=handle)
                    if sub.verdict == 'TESTING' and
                       sub.problem.contestId == vc.contest_id and
                       sub.relativeTimeSeconds <= vc.finish_time - vc.start_time]

        running_subs_flag = any([await has_running_subs(handle) for handle in handles])
        if running_subs_flag:
            msg = 'Some submissions are still being judged'
            await channel.send(embed=discord_common.embed_alert(msg), delete_after=_WATCHING_RATED_VC_WAIT_TIME)
        if now < vc.finish_time or running_subs_flag:
            # Display current standings
            await channel.send(embed=self._make_contest_embed_for_vc_ranklist(ranklist, vc.start_time, vc.finish_time), delete_after=_WATCHING_RATED_VC_WAIT_TIME)
            await self._show_ranklist(channel, vc.contest_id, handles, ranklist=ranklist, vc=True, delete_after=_WATCHING_RATED_VC_WAIT_TIME)
            return
        rating_change_by_handle = {}
        RatingChange = namedtuple('RatingChange', 'handle oldRating newRating')
        for handle, member_id in zip(handles, member_ids):
            delta = ranklist.delta_by_handle.get(handle)
            if delta is None:  # The user did not participate.
                cf_common.user_db.remove_last_ratedvc_participation(member_id)
                continue
            old_rating = cf_common.user_db.get_vc_rating(member_id)
            new_rating = old_rating + delta
            rating_change_by_handle[handle] = RatingChange(handle=handle, oldRating=old_rating, newRating=new_rating)
            cf_common.user_db.update_vc_rating(vc_id, member_id, new_rating)
        cf_common.user_db.finish_rated_vc(vc_id)
        await channel.send(embed=self._make_vc_rating_changes_embed(channel.guild, vc.contest_id, rating_change_by_handle))
        await self._show_ranklist(channel, vc.contest_id, handles, ranklist=ranklist, vc=True)

    @tasks.task_spec(name='WatchRatedVCs',
                     waiter=tasks.Waiter.fixed_delay(_WATCHING_RATED_VC_WAIT_TIME))
    async def _watch_rated_vcs_task(self, _):
        ongoing_rated_vcs = cf_common.user_db.get_ongoing_rated_vc_ids()
        if ongoing_rated_vcs is None:
            return
        for rated_vc_id in ongoing_rated_vcs:
            await self._watch_rated_vc(rated_vc_id)

    @commands.command(brief='Unregister this user from an ongoing ratedvc', usage='@user')
    @commands.has_any_role(constants.TLE_ADMIN, constants.TLE_MODERATOR)
    async def _unregistervc(self, ctx, user: discord.Member):
        """ Unregister this user from an ongoing ratedvc.
        """
        ongoing_vc_member_ids = _get_ongoing_vc_participants()
        if str(user.id) not in ongoing_vc_member_ids:
            raise ContestCogError(f'{user.mention} has no ongoing ratedvc!')
        cf_common.user_db.remove_last_ratedvc_participation(user.id)
        await ctx.send(embed=discord_common.embed_success(f'Successfully unregistered {user.mention} from the ongoing vc.'))

    @commands.command(brief='Set the rated vc channel to the current channel')
    @commands.has_role(constants.TLE_ADMIN)
    async def set_ratedvc_channel(self, ctx):
        """ Sets the rated vc channel to the current channel.
        """
        cf_common.user_db.set_rated_vc_channel(ctx.guild.id, ctx.channel.id)
        await ctx.send(embed=discord_common.embed_success('Rated VC channel saved successfully'))

    @commands.command(brief='Get the rated vc channel')
    async def get_ratedvc_channel(self, ctx):
        """ Gets the rated vc channel.
        """
        channel_id = cf_common.user_db.get_rated_vc_channel(ctx.guild.id)
        channel = ctx.guild.get_channel(channel_id)
        if channel is None:
            raise ContestCogError('There is no rated vc channel')
        embed = discord_common.embed_success('Current rated vc channel')
        embed.add_field(name='Channel', value=channel.mention)
        await ctx.send(embed=embed)

    @commands.command(brief='Show vc ratings')
    async def vcratings(self, ctx):
        users = [(await self.member_converter.convert(ctx, str(member_id)), handle, cf_common.user_db.get_vc_rating(member_id, default_if_not_exist=False))
                 for member_id, handle in cf_common.user_db.get_handles_for_guild(ctx.guild.id)]
        # Filter only rated users. (Those who entered at least one rated vc.)
        users = [(member, handle, rating)
                 for member, handle, rating in users
                 if rating is not None]
        users.sort(key=lambda user: -user[2])

        _PER_PAGE = 10

        def make_page(chunk, page_num):
            style = table.Style('{:>}  {:<}  {:<}  {:<}')
            t = table.Table(style)
            t += table.Header('#', 'Name', 'Handle', 'Rating')
            t += table.Line()
            for index, (member, handle, rating) in enumerate(chunk):
                rating_str = f'{rating} ({cf.rating2rank(rating).title_abbr})'
                t += table.Data(_PER_PAGE * page_num + index, f'{member.display_name}', handle, rating_str)

            table_str = f'```\n{t}\n```'
            embed = discord_common.cf_color_embed(description=table_str)
            return 'VC Ratings', embed

        if not users:
            raise ContestCogError('There are no active VCers.')

        pages = [make_page(chunk, k) for k, chunk in enumerate(paginator.chunkify(users, _PER_PAGE))]
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True)

    @commands.command(brief='Plot vc rating for a list of at most 5 users', usage='@user1 @user2 ..')
    async def vcrating(self, ctx, *members: discord.Member):
        """Plots VC rating for at most 5 users."""
        members = members or (ctx.author, )
        if len(members) > 5:
            raise ContestCogError('Cannot plot more than 5 VCers at once.')
        plot_data = defaultdict(list)

        min_rating = 1100
        max_rating = 1800

        for member in members:
            rating_history = cf_common.user_db.get_vc_rating_history(member.id)
            if not rating_history:
                raise ContestCogError(f'{member.mention} has no vc history.')
            for vc_id, rating in rating_history:
                vc = cf_common.user_db.get_rated_vc(vc_id)
                date = dt.datetime.fromtimestamp(vc.finish_time)
                plot_data[member.display_name].append((date, rating))
                min_rating = min(min_rating, rating)
                max_rating = max(max_rating, rating)

        plt.clf()
        # plot at least from mid gray to mid purple
        for rating_data in plot_data.values():
            x, y = zip(*rating_data)
            plt.plot(x, y,
                     linestyle='-',
                     marker='o',
                     markersize=4,
                     markerfacecolor='white',
                     markeredgewidth=0.5)

        gc.plot_rating_bg(cf.RATED_RANKS)
        plt.gcf().autofmt_xdate()

        plt.ylim(min_rating - 100, max_rating + 200)
        labels = [
            gc.StrWrap('{} ({})'.format(
                member_display_name,
                rating_data[-1][1]))
            for member_display_name, rating_data in plot_data.items()
        ]
        plt.legend(labels, loc='upper left', prop=gc.fontprop)

        discord_file = gc.get_current_figure_as_file()
        embed = discord_common.cf_color_embed(title='VC rating graph')
        discord_common.attach_image(embed, discord_file)
        discord_common.set_author_footer(embed, ctx.author)
        await ctx.send(embed=embed, file=discord_file)

    @discord_common.send_error_if(ContestCogError, rl.RanklistError,
                                  cache_system2.CacheError, cf_common.ResolveHandleError)
    async def cog_command_error(self, ctx, error):
        pass

    @commands.command(brief='Plot vc performance for a list of at most 5 users', aliases=['vcperf'], usage='@user1 @user2 ..')
    async def vcperformance(self, ctx, *members: discord.Member):
        """Plots VC performance for at most 5 users."""
        members = members or (ctx.author, )
        if len(members) > 5:
            raise ContestCogError('Cannot plot more than 5 VCers at once.')
        plot_data = defaultdict(list)

        min_rating = 1100
        max_rating = 1800

        for member in members:
            rating_history = cf_common.user_db.get_vc_rating_history(member.id)
            if not rating_history:
                raise ContestCogError(f'{member.mention} has no vc history.')
            ratingbefore = 1500
            for vc_id, rating in rating_history:
                vc = cf_common.user_db.get_rated_vc(vc_id)
                perf = ratingbefore + (rating - ratingbefore)*4
                date = dt.datetime.fromtimestamp(vc.finish_time)
                plot_data[member.display_name].append((date, perf))
                min_rating = min(min_rating, perf)
                max_rating = max(max_rating, perf)
                ratingbefore = rating

        plt.clf()
        # plot at least from mid gray to mid purple
        for rating_data in plot_data.values():
            x, y = zip(*rating_data)
            plt.plot(x, y,
                     linestyle='-',
                     marker='o',
                     markersize=4,
                     markerfacecolor='white',
                     markeredgewidth=0.5)

        gc.plot_rating_bg(cf.RATED_RANKS)
        plt.gcf().autofmt_xdate()

        plt.ylim(min_rating - 100, max_rating + 200)
        labels = [
            gc.StrWrap('{} ({})'.format(
                member_display_name,
                ratingbefore))
            for member_display_name, rating_data in plot_data.items()
        ]
        plt.legend(labels, loc='upper left', prop=gc.fontprop)

        discord_file = gc.get_current_figure_as_file()
        embed = discord_common.cf_color_embed(title='VC performance graph')
        discord_common.attach_image(embed, discord_file)
        discord_common.set_author_footer(embed, ctx.author)
        await ctx.send(embed=embed, file=discord_file)


    @commands.command(brief='Estimation of contest problem ratings', aliases=['probrat'], usage='contest_id')
    async def problemratings(self, ctx, contest_id: int):
        """Estimation of contest problem ratings
        """
        await ctx.send('This will take a while')
        contests = await cf.contest.list()
        reqcontest = [contest for contest in contests if contest.id == contest_id]
        combined = [contest for contest in contests if reqcontest[0].startTimeSeconds == contest.startTimeSeconds]


        # get ranklist of all contests in separate lists
        # get rating_changes of all contests in separate lists
        # for each problem name of original contest
            # find in each ranklist the handles and ratings that had a chance to do the problem
            # calculate rating from these values

        problems = []
        ranklists = []
        rating_cache = dict()
        for contest in combined:
            _, problem, ranklist = await cf.contest.standings(contest_id=contest.id, show_unofficial=False)
            problems.append(problem)
            ranklists.append(ranklist)

            if contest.id == contest_id:
                officialRatings = [prob.rating for prob in problem]
                indicies = [prob.index for prob in problem]
                problemNames = [prob.name for prob in problem]

            #build ratingCache that has all old_rating for all contestants
            try:
                rating_change = await cf.contest.ratingChanges(contest_id=contest.id)
            except cf.RatingChangesUnavailableError as e:
                rating_change = []
            from_cache = False
            if len(rating_change) == 0:
                # get rating of contestants from cache
                # we want to have the rating before the contest we query for
                from_cache = True
                cached_ratings = await cf_common.cache2.rating_changes_cache.get_all_ratings_before_timestamp(reqcontest[0].startTimeSeconds)
                for row in ranklist:
                    member = row.party.members[0].handle
                    # members not in cache are considered new (Unrated)
                    if member in cached_ratings:
                        rating_cache[member] = cached_ratings[member].newRating
                    else:
                        rating_cache[member] = 0
            else:
                for change in rating_change:
                    rating_cache[change.handle] = change.oldRating

        def calculateDifficulty(ratings, solved):
            ans = -1000

            def calcProb(dif):
                prob = 1
                d = 0
                for (r, s) in zip(ratings, solved):
                    p = 1/(1+10**((dif-r)/400))
                    d += p
                    if s:
                        d -= 1
                    prob *= p if s else (1-p)
                return d > 0 and prob < 0.95
            jump = 4096
            while jump >= 1:
                if calcProb(ans+jump):
                    ans += jump
                jump /= 2
            ans = round(ans+1)
            return ans

        predicted = []
        for name in problemNames:
            ratings = []
            solves = []

            for i in range(len(problems)):
                #get index of name in problem list of each contest
                idx = -1
                for j in range(len(problems[i])):
                    if problems[i][j].name == name:
                        idx = j
                if idx == -1: continue
                for row in ranklists[i]:
                    member = row.party.members[0].handle
                    if member in rating_cache:
                        solves.append(min(row.problemResults[idx].points, 1))
                        ratings.append(rating_cache[member])
            predicted.append(calculateDifficulty(ratings,solves))

        # Output results
        style = table.Style('{:<}  {:>}  {:>}')
        t = table.Table(style)
        t += table.Header('#', 'Official', 'Predicted (C)' if from_cache else 'Predicted')
        t += table.Line()
        for i, index in enumerate(indicies):
            t += table.Data(f'{index}', f'{officialRatings[i]}', f'{predicted[i]}')
        table_str = f'```\n{t}\n```'
        url = f'{cf.CONTEST_BASE_URL}{contest_id}'
        title = reqcontest[0].name
        embed = discord_common.cf_color_embed(description=table_str, title=title, url=url)
        await ctx.send(embed=embed)

    @discord_common.send_error_if(ContestCogError, rl.RanklistError,
                                  cache_system2.CacheError, cf_common.ResolveHandleError)
    async def cog_command_error(self, ctx, error):
        pass



async def setup(bot):
    await bot.add_cog(Contests(bot))
