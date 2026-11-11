/* =========================================================
   BXSCI Athletics — UI + calendar logic
   Depends on data.js (window.BXSData).

   Dates are handled as plain "YYYY-MM-DD" strings in
   America/New_York. Date math uses UTC-midnight Date objects
   only for arithmetic, so the visitor's own time zone can never
   shift a game onto the wrong day.
   ========================================================= */

(function () {
  'use strict';

  const TZ = 'America/New_York';

  /* ---------------------------------------------------------
     Date helpers
     --------------------------------------------------------- */
  const pad = n => String(n).padStart(2, '0');
  const ymd = (y, m, d) => `${y}-${pad(m)}-${pad(d)}`;
  const parts = iso => { const [y, m, d] = iso.split('-').map(Number); return { y, m, d }; };
  const toUTC = iso => { const { y, m, d } = parts(iso); return new Date(Date.UTC(y, m - 1, d)); };
  const fromUTC = dt => ymd(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate());

  function addDays(iso, n) { const dt = toUTC(iso); dt.setUTCDate(dt.getUTCDate() + n); return fromUTC(dt); }
  function weekdayIndex(iso) { return (toUTC(iso).getUTCDay() + 6) % 7; } // Monday = 0 … Sunday = 6
  function startOfWeek(iso) { return addDays(iso, -weekdayIndex(iso)); } // always a Monday
  function daysInMonth(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }
  function startOfMonth(iso) { const { y, m } = parts(iso); return ymd(y, m, 1); }
  function endOfMonth(iso) { const { y, m } = parts(iso); return ymd(y, m, daysInMonth(y, m)); }
  function addMonths(iso, n) {
    const { y, m, d } = parts(iso);
    const idx = y * 12 + (m - 1) + n;
    const ny = Math.floor(idx / 12), nm = (idx % 12) + 1;
    return ymd(ny, nm, Math.min(d, daysInMonth(ny, nm))); // Jan 31 + 1 month -> Feb 28
  }
  function diffDays(a, b) { return Math.round((toUTC(b) - toUTC(a)) / 864e5); }
  function sameMonth(a, b) { return a.slice(0, 7) === b.slice(0, 7); }

  const fmtCache = {};
  function fmt(iso, opts) {
    const key = JSON.stringify(opts);
    if (!fmtCache[key]) fmtCache[key] = new Intl.DateTimeFormat('en-US', Object.assign({ timeZone: 'UTC' }, opts));
    return fmtCache[key].format(toUTC(iso));
  }
  const monthShort = iso => fmt(iso, { month: 'short' });
  const dayNum = iso => parts(iso).d;

  /** Current date + time in New York, regardless of the visitor's time zone. */
  function nowET() {
    const p = {};
    new Intl.DateTimeFormat('en-US', {
      timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).formatToParts(new Date()).forEach(x => { p[x.type] = x.value; });
    const hour = p.hour === '24' ? '00' : p.hour;
    return { date: `${p.year}-${p.month}-${p.day}`, time: `${hour}:${p.minute}` };
  }

  function splitTime(t) {
    if (!t) return { main: 'TBA', mer: '' };
    let [h, m] = t.split(':').map(Number);
    const mer = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return { main: `${h}:${pad(m)}`, mer };
  }
  const timeLabel = t => { const s = splitTime(t); return s.mer ? `${s.main} ${s.mer}` : 'Time TBA'; };

  /* ---------------------------------------------------------
     State
     --------------------------------------------------------- */
  const store = {
    get(k, fallback) { try { return localStorage.getItem('bxs:' + k) || fallback; } catch (e) { return fallback; } },
    set(k, v) { try { localStorage.setItem('bxs:' + k, v); } catch (e) { /* storage unavailable */ } }
  };

  const params = new URLSearchParams(location.search);
  const initialDate = /^\d{4}-\d{2}-\d{2}$/.test(params.get('date') || '') ? params.get('date') : nowET().date;

  const state = {
    data: null,
    error: null,
    view: store.get('view', 'week') === 'month' ? 'month' : 'week',
    layout: store.get('layout', 'list') === 'calendar' ? 'calendar' : 'list',
    selected: initialDate,         // the date that anchors the visible week/month
    filters: { sport: 'all', gender: 'all', level: 'all', ha: 'all' },
    showAllResults: false,
    lastFetch: 0,
    refreshFailed: false
  };

  /* ---------------------------------------------------------
     Game helpers
     --------------------------------------------------------- */
  const SPORT_ORDER = ['Soccer', 'Volleyball', 'Swimming', 'Cross Country', 'Tennis', 'Football', 'Basketball',
    'Fencing', 'Bowling', 'Indoor Track', 'Wrestling', 'Gymnastics', 'Baseball', 'Softball', 'Lacrosse',
    'Outdoor Track', 'Handball', 'Badminton', 'Table Tennis', 'Golf', 'Volleyball (Boys)', 'Stunt', 'Rugby'];
  const SPORT_ICON = {
    'Soccer': 's-soccer', 'Volleyball': 's-volleyball', 'Swimming': 's-swim', 'Cross Country': 's-flag',
    'Indoor Track': 's-flag', 'Outdoor Track': 's-flag', 'Tennis': 's-tennis', 'Table Tennis': 's-tennis',
    'Basketball': 's-basketball', 'Fencing': 's-fencing', 'Bowling': 's-bowling'
  };
  const SPORT_ABBR = {
    'Soccer': 'Soc', 'Volleyball': 'VB', 'Swimming': 'Swim', 'Cross Country': 'XC', 'Tennis': 'Ten',
    'Basketball': 'BB', 'Fencing': 'Fence', 'Bowling': 'Bowl', 'Indoor Track': 'Track', 'Outdoor Track': 'Track',
    'Baseball': 'Base', 'Softball': 'Soft', 'Lacrosse': 'Lax', 'Wrestling': 'Wrest', 'Table Tennis': 'TT',
    'Handball': 'HB', 'Badminton': 'Badm', 'Gymnastics': 'Gym', 'Golf': 'Golf', 'Football': 'FB'
  };
  const sportIcon = s => SPORT_ICON[s] || 's-generic';
  const sportAbbr = s => SPORT_ABBR[s] || s.slice(0, 5);
  const sportRank = s => { const i = SPORT_ORDER.indexOf(s); return i === -1 ? 999 : i; };

  /**
   * What should a game show right now?
   *   upcoming   – scheduled and hasn't started yet
   *   final      – PSAL published a score
   *   finalText  – meet/championship with a placing instead of a score
   *   awaiting   – start time has passed but no score is published
   *   postponed / canceled
   */
  function resultOf(g, now) {
    if (g.status === 'canceled') return { kind: 'canceled' };
    if (g.status === 'postponed') return { kind: 'postponed' };
    if (g.status === 'final' && g.score) {
      const a = g.score.bxsci, b = g.score.opponent;
      return { kind: 'final', outcome: a > b ? 'W' : a < b ? 'L' : 'T', a, b };
    }
    if (g.status === 'final' && g.resultText) return { kind: 'finalText' };
    if (g.status === 'final') return { kind: 'awaiting' };
    const started = g.date < now.date || (g.date === now.date && (g.time || '23:59') <= now.time);
    return started ? { kind: 'awaiting' } : { kind: 'upcoming' };
  }

  function matchesFilters(g, f) {
    return (f.sport === 'all' || g.sport === f.sport)
      && (f.gender === 'all' || g.gender === f.gender || g.gender === 'Coed')
      && (f.level === 'all' || g.level === f.level)
      && (f.ha === 'all' || g.homeAway === f.ha);
  }
  const filtersActive = () => Object.values(state.filters).some(v => v !== 'all');

  const teamLine = g => [g.gender === 'Coed' ? 'Coed' : g.gender, g.level, g.sport].join(' · ') + (g.round ? ' · ' + g.round : '');
  const oppName = g => g.opponent || g.event || 'TBA';
  const oppLine = g => g.opponent ? (g.homeAway === 'away' ? '@ ' : 'vs ') + g.opponent : (g.event || 'Opponent TBA');

  function gamesBetween(start, end) {
    return state.data.games.filter(g => g.date >= start && g.date <= end);
  }

  /* Sports offered as filters = every sport whose ACTUAL season overlaps the visible week/month.
     A team's season is its first → last game in that school year (Aug–Jul), taken from the
     collected PSAL schedules, plus a few days on each side for the transition between seasons.
     So a sport stays available all season even in a week with no games, and overlapping
     seasons (fall playoffs + early winter games) both show. No fixed calendar dates are used. */
  const SEASON_PAD_DAYS = 7;
  let seasonCache = { games: null, spans: [] };
  function teamSeasons() {
    const games = state.data.games;
    if (seasonCache.games === games) return seasonCache.spans;
    const spans = new Map();
    for (const g of games) {
      const { y, m } = parts(g.date);
      const key = [g.sport, g.gender, g.level, m >= 8 ? y : y - 1].join('|');
      const s = spans.get(key);
      if (!s) spans.set(key, { sport: g.sport, first: g.date, last: g.date });
      else { if (g.date < s.first) s.first = g.date; if (g.date > s.last) s.last = g.date; }
    }
    seasonCache = { games, spans: [...spans.values()] };
    return seasonCache.spans;
  }
  function sportsInSeason(start, end) {
    const out = new Set();
    for (const s of teamSeasons()) {
      if (addDays(s.first, -SEASON_PAD_DAYS) <= end && addDays(s.last, SEASON_PAD_DAYS) >= start) out.add(s.sport);
    }
    return out;
  }

  function visibleRange() {
    if (state.view === 'week') {
      const start = startOfWeek(state.selected);
      return { start, end: addDays(start, 6) };
    }
    return { start: startOfMonth(state.selected), end: endOfMonth(state.selected) };
  }

  function rangeLabel(start, end) {
    if (state.view === 'month') return fmt(start, { month: 'long', year: 'numeric' });
    const s = parts(start), e = parts(end);
    if (s.y !== e.y) return `${monthShort(start)} ${s.d}, ${s.y} – ${monthShort(end)} ${e.d}, ${e.y}`;
    if (s.m !== e.m) return `${monthShort(start)} ${s.d} – ${monthShort(end)} ${e.d}, ${e.y}`;
    return `${monthShort(start)} ${s.d} – ${e.d}, ${e.y}`;
  }

  function seasonFor(iso) {
    const seasons = state.data ? state.data.meta.seasons : [];
    const hit = seasons.find(s => iso >= s.start && iso <= s.end);
    if (hit) return hit.name;
    const m = parts(iso).m;
    return m >= 9 && m <= 11 ? 'Fall season' : (m === 12 || m <= 2) ? 'Winter season' : m <= 6 ? 'Spring season' : 'Summer break';
  }

  /* ---------------------------------------------------------
     HTML builders
     --------------------------------------------------------- */
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icon = (id, cls = 'i') => `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
  const SPARKLE = '<path d="M12 1.5c.6 5.6 3.4 9.4 10.5 10.5-7.1 1.1-9.9 4.9-10.5 10.5-.6-5.6-3.4-9.4-10.5-10.5C8.6 10.9 11.4 7.1 12 1.5Z"/>';
  const sparkle = cls => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${SPARKLE}</svg>`;

  function haPill(g) {
    if (g.homeAway === 'home') return `<span class="ha ha-home">${icon('i-home')}Home</span>`;
    if (g.homeAway === 'away') return `<span class="ha ha-away">${icon('i-away')}Away</span>`;
    return `<span class="ha ha-neutral">${icon('i-neutral')}Neutral</span>`;
  }

  function statusHTML(g, r) {
    switch (r.kind) {
      case 'final': {
        const word = { W: 'Win', L: 'Loss', T: 'Tie' }[r.outcome];
        const label = `${word}. Bronx Science ${r.a}, ${oppName(g)} ${r.b}`;
        return `<span class="s-label">Final</span>
          <span class="score score-${r.outcome}" role="img" aria-label="${esc(label)}" title="${esc(label)}">
            <i>${r.outcome}</i><b>${r.a} – ${r.b}</b></span>`;
      }
      case 'finalText':
        return `<span class="s-label">Final</span><span class="score score-T score-text"><b>${esc(g.resultText)}</b></span>`;
      case 'awaiting':
        return `<span class="spill spill-wait">${icon('i-hourglass')}Awaiting result</span><span class="s-note">Not yet posted by PSAL</span>`;
      case 'postponed':
        return `<span class="spill spill-off">${icon('i-pause')}Postponed</span><span class="s-note">${esc(g.statusNote || 'New date TBA')}</span>`;
      case 'canceled':
        return `<span class="spill spill-off">${icon('i-x')}Canceled</span>${g.statusNote ? `<span class="s-note">${esc(g.statusNote)}</span>` : ''}`;
      default:
        return `<span class="spill spill-up">${icon('i-clock')}Upcoming</span>`;
    }
  }

  function gameCard(g, opts = {}) {
    const now = nowET();
    const r = resultOf(g, now);
    const cls = ['game'];
    if (r.kind === 'final' || r.kind === 'finalText' || r.kind === 'awaiting') cls.push('is-done');
    if (r.kind === 'postponed' || r.kind === 'canceled') cls.push('is-off');
    if (r.kind === 'final') cls.push('is-' + r.outcome);
    if (opts.showDate) cls.push('show-date');
    const t = splitTime(g.time);
    const dateTxt = fmt(g.date, { weekday: 'short', month: 'short', day: 'numeric' });
    return `<div class="gc"><article class="${cls.join(' ')}" data-id="${esc(g.id)}">
      <div class="g-time">${icon('i-clock')}<span class="g-when"><span class="g-date">${esc(dateTxt)} · </span><b>${t.main}</b><small>${t.mer ? ' ' + t.mer : ''}</small></span></div>
      <div class="g-sport"><span class="tile" title="${esc(g.sport)}">${icon(sportIcon(g.sport))}</span></div>
      <div class="g-head"><p class="g-team">${esc(teamLine(g))}</p><h4 class="g-opp">${esc(oppLine(g))}</h4></div>
      <div class="g-loc">${icon('i-pin')}<span>${esc(g.location)}</span></div>
      <div class="g-ha">${haPill(g)}</div>
      <div class="g-status">${statusHTML(g, r)}</div>
    </article></div>`;
  }

  function emptyDay(title, text) {
    return `<div class="empty-day"><span class="empty-ic">${sparkle('')}</span>
      <div><h4>${esc(title)}</h4><p>${esc(text)}</p></div></div>`;
  }

  /* ---------------------------------------------------------
     Renderers
     --------------------------------------------------------- */
  const $ = id => document.getElementById(id);

  function render() {
    renderControls();
    renderSync();
    if (state.error && !state.data) { renderError(); return; }
    if (!state.data) return;
    renderNextUp();
    renderBody();
    renderResults();
  }

  function renderControls() {
    const { start, end } = visibleRange();
    const label = rangeLabel(start, end);
    $('rangeLabel').textContent = label;
    const season = state.data ? seasonFor(state.selected) : '';
    $('heroRange').textContent = state.view === 'week'
      ? `Week of ${label.replace(/, \d{4}$/, '')}${season ? '  ·  ' + season : ''}`
      : `${label}${season ? '  ·  ' + season : ''}`;

    const unit = state.view === 'week' ? 'week' : 'month';
    $('prevBtn').setAttribute('aria-label', `Previous ${unit}`);
    $('nextBtn').setAttribute('aria-label', `Next ${unit}`);
    const today = nowET().date;
    $('todayBtn').classList.toggle('is-current', today >= start && today <= end && state.selected === today);

    document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === state.view)));
    document.querySelectorAll('[data-layout]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.layout === state.layout)));
    document.querySelectorAll('[data-filter]').forEach(b => b.setAttribute('aria-pressed', String(state.filters[b.dataset.filter] === b.dataset.value)));
    $('clearBtn').hidden = !filtersActive();

    // Sport chips: every sport in season during the visible week/month (see sportsInSeason),
    // all fully clickable — a sport with no game in this exact week is NOT grayed out.
    if (state.data) {
      const sports = sportsInSeason(start, end);
      gamesBetween(start, end).forEach(g => sports.add(g.sport));
      if (state.filters.sport !== 'all') sports.add(state.filters.sport);
      const list = [...sports].sort((a, b) => sportRank(a) - sportRank(b) || a.localeCompare(b));
      $('sportChips').innerHTML = ['all', ...list].map(s => {
        const pressed = state.filters.sport === s;
        return `<button class="chip" type="button" data-sport="${esc(s)}" aria-pressed="${pressed}">${s === 'all' ? 'All sports' : esc(s)}</button>`;
      }).join('');
    }
  }

  function renderBody() {
    const body = $('scheduleBody');
    const { start, end } = visibleRange();
    const games = gamesBetween(start, end).filter(g => matchesFilters(g, state.filters));
    const unit = state.view === 'week' ? 'week' : 'month';
    $('countText').textContent = `${games.length} ${games.length === 1 ? 'game' : 'games'} this ${unit}`
      + (state.data.meta.isSample ? '  ·  Sample data' : '') + '  ·  Scores: Bronx Science first';

    if (state.layout === 'calendar') { body.innerHTML = calendarHTML(start, end, games); return; }
    if (!games.length) { body.innerHTML = bigEmptyHTML(end); return; }
    body.innerHTML = listHTML(start, end, games);
  }

  function listHTML(start, end, games) {
    const today = nowET().date;
    const notes = state.data.meta.dayNotes;
    const byDate = groupByDate(games);
    // Week view: every day Monday–Sunday. Month view: only days that have games (or a note).
    const days = [];
    for (let d = start; d <= end; d = addDays(d, 1)) {
      if (state.view === 'week' || byDate[d] || notes[d]) days.push(d);
    }
    return `<div class="day-list">${days.map(d => {
      const list = byDate[d] || [];
      const isToday = d === today;
      const quiet = !list.length && !notes[d];
      const count = list.length
        ? `${list.length} ${list.length === 1 ? 'game' : 'games'}`
        : (notes[d] ? notes[d].split(' — ')[0] : 'No games');
      let inner = '';
      if (!list.length && notes[d]) {
        const [title, sub] = notes[d].split(' — ');
        inner = emptyDay('No games today', sub ? sub.charAt(0).toUpperCase() + sub.slice(1) + ' — enjoy the break.' : title);
      }
      inner += list.map(g => gameCard(g)).join('');
      return `<div class="day${isToday ? ' is-today' : ''}${quiet ? ' is-quiet' : ''}" data-date="${d}">
        <div class="day-badge"><small>${fmt(d, { weekday: 'short' })}</small><b>${dayNum(d)}</b></div>
        <div class="day-games">
          <div class="day-head">
            <span class="mini-badge">${dayNum(d)}</span>
            <div class="day-title">
              <h3>${fmt(d, { weekday: 'long', month: 'short', day: 'numeric' })}</h3>
              ${isToday ? '<span class="today-tag">Today</span>' : ''}
              <span class="day-count">${esc(count)}</span>
            </div>
          </div>
          ${inner}
        </div>
      </div>`;
    }).join('')}</div>`;
  }

  function calendarHTML(start, end, games) {
    const today = nowET().date;
    const notes = state.data.meta.dayNotes;
    const byDate = groupByDate(games);
    const gridStart = startOfWeek(start);
    const gridEnd = addDays(startOfWeek(end), 6);
    const isWeek = state.view === 'week';
    const now = nowET();

    const head = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
      .map(d => `<span><span class="full">${d.slice(0, 3)}</span><span class="short">${d[0]}</span></span>`).join('');

    let rows = '';
    for (let w = gridStart; w <= gridEnd; w = addDays(w, 7)) {
      let cells = '';
      for (let i = 0; i < 7; i++) {
        const d = addDays(w, i);
        const out = !isWeek && !sameMonth(d, start);
        const list = byDate[d] || [];
        const limit = isWeek ? list.length : 2;
        const evs = list.slice(0, limit).map(g => eventPill(g, now)).join('')
          + (list.length > limit ? `<span class="ev-more">+${list.length - limit} more</span>` : '');
        const note = !list.length && notes[d] ? `<span class="cell-note">${esc(notes[d].split(' — ')[0])}</span>` : '';
        const cls = ['cell', out && 'is-out', d === today && 'is-today', d === state.selected && 'is-selected'].filter(Boolean).join(' ');
        const aria = `${fmt(d, { weekday: 'long', month: 'long', day: 'numeric' })}, ${list.length ? list.length + (list.length === 1 ? ' game' : ' games') : 'no games'}`;
        cells += `<button type="button" class="${cls}" data-date="${d}" aria-label="${esc(aria)}" aria-pressed="${d === state.selected}">
          <span class="cell-num">${dayNum(d)}</span><span class="cell-events">${evs}</span>${note}</button>`;
      }
      rows += `<div class="cal-row">${cells}</div>`;
    }

    // Selected-day panel
    const sel = state.selected;
    const selGames = byDate[sel] || [];
    const panelBody = selGames.length
      ? selGames.map(g => gameCard(g, { showDate: false })).join('')
      : emptyDay('No games this day', notes[sel] ? notes[sel] : (filtersActive() ? 'Nothing matches your filters on this date.' : 'Pick another day to see its games.'));
    const panel = `<div class="panel">
      <div class="panel-head">
        <div><span class="overline">Selected day</span><h3>${fmt(sel, { weekday: 'long', month: 'short', day: 'numeric' })}</h3></div>
        <span class="count-chip">${selGames.length} ${selGames.length === 1 ? 'game' : 'games'}</span>
      </div>
      ${panelBody}
      <p class="tip">Tap any day to see its games. Colors show results at a glance.</p>
    </div>`;

    const emptyRange = !games.length ? `<div class="cal-empty">${bigEmptyHTML(end)}</div>` : '';
    return `<div class="cal-layout"><div class="cal ${isWeek ? 'cal-week' : 'cal-month'}">
      <div class="cal-head">${head}</div>${rows}</div>${panel}</div>${emptyRange ? '<div style="height:20px"></div>' + emptyRange : ''}`;
  }

  function eventPill(g, now) {
    const r = resultOf(g, now);
    const abbr = (g.level === 'JV' ? 'JV ' : '') + sportAbbr(g.sport);
    let cls = 'ev-up', text = `${splitTime(g.time).mer ? splitTime(g.time).main : 'TBA'} ${abbr}`;
    if (r.kind === 'final') { cls = 'ev-' + r.outcome; text = `${r.outcome} ${r.a}–${r.b} ${abbr}`; }
    else if (r.kind === 'finalText') { cls = 'ev-T'; text = `${abbr} · ${g.resultText}`; }
    else if (r.kind === 'awaiting') { cls = 'ev-wait'; text = `Awaiting ${abbr}`; }
    else if (r.kind === 'postponed' || r.kind === 'canceled') { cls = 'ev-off'; text = abbr; }
    const title = `${teamLine(g)} ${oppLine(g)} — ${timeLabel(g.time)}`;
    return `<span class="ev ${cls}" title="${esc(title)}"><i class="dot"></i><span>${esc(text)}</span></span>`;
  }

  function bigEmptyHTML(rangeEnd) {
    const unit = state.view === 'week' ? 'week' : 'month';
    const nextGame = state.data.games.find(g => g.date > rangeEnd && matchesFilters(g, state.filters));
    const onlySport = state.filters.sport !== 'all'
      && Object.entries(state.filters).every(([k, v]) => k === 'sport' || v === 'all');
    const title = onlySport ? `No games scheduled this ${unit}`
      : filtersActive() ? 'No games match your filters' : `No games this ${unit}`;
    const text = onlySport
      ? `${state.filters.sport} has no Bronx Science games scheduled this ${unit}.${nextGame ? ' Jump to its next game below.' : ''}`
      : filtersActive()
      ? `Nothing on the schedule for these filters this ${unit}. Try widening them or jump ahead.`
      : `Nothing on the Bronx Science schedule for these dates — a good ${unit} to rest up.`;
    const actions = [
      filtersActive() ? '<button class="soft-btn" type="button" data-action="clear">Clear filters</button>' : '',
      nextGame ? `<button class="soft-btn is-primary" type="button" data-action="jump" data-date="${nextGame.date}">Next game: ${fmt(nextGame.date, { month: 'short', day: 'numeric' })} ${icon('i-arrow')}</button>` : ''
    ].join('');
    return `<div class="empty-big"><span class="empty-ic">${sparkle('')}</span><h4>${title}</h4><p>${text}</p>
      ${actions ? `<div class="empty-actions">${actions}</div>` : ''}</div>`;
  }

  function renderNextUp() {
    const now = nowET();
    const g = state.data.games.find(x => resultOf(x, now).kind === 'upcoming');
    const box = $('nextUp');
    if (!g) {
      box.innerHTML = `<div class="next"><div class="next-top"><span class="next-label">Next up</span></div>
        <p class="next-empty">No upcoming games on the schedule yet. Check back soon!</p>${sparkle('sparkle sparkle-a')}</div>`;
      return;
    }
    const days = diffDays(now.date, g.date);
    const countdown = days === 0 ? 'Today' : days === 1 ? 'Tomorrow' : `in ${days} days`;
    box.innerHTML = `<a class="next" href="#schedule" data-action="jump" data-date="${g.date}" aria-label="Next up: ${esc(teamLine(g) + ' ' + oppLine(g))}, ${esc(fmt(g.date, { weekday: 'long', month: 'long', day: 'numeric' }))}">
      <div class="next-top"><span class="next-label">Next up</span><span class="countdown">${countdown}</span></div>
      <div class="next-game"><span class="tile">${icon(sportIcon(g.sport))}</span>
        <div><p class="next-team" data-countdown="${countdown}">${esc(teamLine(g))}</p><p class="next-opp">${esc(oppLine(g))}</p></div></div>
      <div class="next-meta">
        <span>${icon('i-clock')}${esc(fmt(g.date, { weekday: 'short', month: 'short', day: 'numeric' }))} · ${timeLabel(g.time)}</span>
        <span>${icon('i-pin')}${esc(g.location)}</span>
      </div>
      ${sparkle('sparkle sparkle-a')}${sparkle('sparkle sparkle-b')}
    </a>`;
  }

  function renderResults() {
    const now = nowET();
    const finals = state.data.games
      .filter(g => { const k = resultOf(g, now).kind; return k === 'final' || k === 'finalText'; })
      .sort((a, b) => (b.date + (b.time || '')).localeCompare(a.date + (a.time || '')));
    const shown = state.showAllResults ? finals : finals.slice(0, 6);
    $('resultsGrid').innerHTML = shown.length
      ? shown.map(g => gameCard(g, { showDate: true })).join('')
      : emptyDay('No results yet', 'Final scores will appear here once PSAL posts them.');
    const btn = $('resultsToggle');
    btn.hidden = finals.length <= 6;
    btn.classList.toggle('is-open', state.showAllResults);
    btn.querySelector('span').textContent = state.showAllResults ? 'Show fewer' : `All results (${finals.length})`;
  }

  function relativeTime(iso) {
    const mins = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
    if (mins < 1) return { long: 'just now', short: 'now' };
    if (mins < 60) return { long: `${mins} min ago`, short: `${mins}m ago` };
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return { long: `${hrs} hr ago`, short: `${hrs}h ago` };
    const days = Math.round(hrs / 24);
    return { long: `${days} day${days === 1 ? '' : 's'} ago`, short: `${days}d ago` };
  }

  // How old can live data get before we flag it? Syncs run every 30 min, but
  // GitHub can delay scheduled runs, so allow a generous margin.
  const STALE_AFTER_MS = 2 * 3600e3;
  const SOURCE_LABEL = { psal: 'Synced with PSAL', manual: 'Updated manually', sample: 'Sample data' };

  function renderSync() {
    const dot = $('syncDot');
    dot.className = 'sync-dot';
    if (state.error && !state.data) {
      dot.classList.add('is-error');
      $('syncText').textContent = 'Couldn’t load schedule';
      $('syncShort').textContent = 'Offline';
      $('footSync').textContent = 'Schedule data unavailable';
      return;
    }
    if (!state.data) return;
    const meta = state.data.meta;
    if (!meta.lastSynced) {
      dot.classList.add('is-stale');
      $('syncText').textContent = 'Last update time unknown';
      $('syncShort').textContent = '—';
      $('footSync').textContent = 'Last updated: unknown';
      return;
    }
    const rel = relativeTime(meta.lastSynced);
    const abs = new Intl.DateTimeFormat('en-US', { timeZone: TZ, month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' })
      .format(new Date(meta.lastSynced)) + ' ET';
    const age = Date.now() - Date.parse(meta.lastSynced);
    const stale = meta.source === 'psal' ? age > STALE_AFTER_MS : age > 7 * 24 * 3600e3;
    if (meta.isSample) dot.classList.add('is-sample');
    else if (state.refreshFailed) dot.classList.add('is-error');
    else if (stale) dot.classList.add('is-stale');
    const source = SOURCE_LABEL[meta.source] || 'Last updated';
    const verb = meta.source === 'psal' ? '' : 'updated ';
    $('syncText').textContent = `${source} · ${verb}${rel.long}`;
    $('syncShort').textContent = meta.isSample ? 'Sample' : rel.short;
    let tip = `${meta.source === 'psal' ? 'Last successful PSAL sync' : 'Last updated'} ${abs}.`;
    if (meta.isSample) tip += ' Sample data — not real PSAL results.';
    else if (stale && meta.source === 'psal') tip += ' PSAL may be unreachable right now; showing the most recent data we have.';
    if (state.refreshFailed) tip += ' Couldn’t check for new data just now.';
    $('syncBtn').title = tip + ' Click to check for updates.';
    $('footSync').textContent = `${meta.source === 'psal' ? 'Last synced with PSAL' : 'Last updated'} ${abs}${meta.isSample ? ' · sample data' : ''}`;
    $('menuMeta').textContent = `${source} · ${abs}`;
    $('footNote').textContent = meta.isSample
      ? 'Showing sample data for testing — not real PSAL schedules or results. Times are Eastern.'
      : meta.source === 'psal'
        ? 'Unofficial copy of public PSAL schedules — psal.org is the official source. Scores are Bronx Science first; times are Eastern.'
        : 'Scores are shown Bronx Science first. All times are Eastern Time.';
  }

  function renderError() {
    const fileProtocol = location.protocol === 'file:';
    $('scheduleBody').innerHTML = `<div class="notice"><h4>We couldn’t load the schedule</h4>
      <p>${fileProtocol
        ? 'Browsers block <code>games.json</code> when a page is opened straight from your files. Open the folder in VS Code and use <b>Live Server</b> (see README).'
        : 'Please check your connection and try the refresh button above.'}</p></div>`;
    $('nextUp').innerHTML = '';
    $('resultsGrid').innerHTML = '';
    $('countText').textContent = '';
  }

  function groupByDate(games) {
    const out = {};
    games.forEach(g => { (out[g.date] = out[g.date] || []).push(g); });
    return out;
  }

  /* ---------------------------------------------------------
     Events
     --------------------------------------------------------- */
  function shift(dir) {
    state.selected = state.view === 'week' ? addDays(state.selected, 7 * dir) : addMonths(state.selected, dir);
    render();
  }

  function jumpTo(date) {
    state.selected = date;
    render();
    $('schedule').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function bind() {
    $('prevBtn').addEventListener('click', () => shift(-1));
    $('nextBtn').addEventListener('click', () => shift(1));
    $('todayBtn').addEventListener('click', () => { state.selected = nowET().date; render(); });

    document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => {
      state.view = b.dataset.view; store.set('view', state.view); render(); // selected date is kept
    }));
    document.querySelectorAll('[data-layout]').forEach(b => b.addEventListener('click', () => {
      state.layout = b.dataset.layout; store.set('layout', state.layout); render();
    }));
    document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => {
      state.filters[b.dataset.filter] = b.dataset.value; render();
    }));
    $('sportChips').addEventListener('click', e => {
      const chip = e.target.closest('[data-sport]');
      if (!chip) return;
      state.filters.sport = chip.dataset.sport; render();
    });
    $('clearBtn').addEventListener('click', clearFilters);

    // Delegated: calendar cells, empty-state buttons, next-up card
    document.addEventListener('click', e => {
      const cell = e.target.closest('.cell[data-date]');
      if (cell) { state.selected = cell.dataset.date; render(); return; }
      const act = e.target.closest('[data-action]');
      if (!act) return;
      if (act.dataset.action === 'clear') clearFilters();
      if (act.dataset.action === 'jump') { e.preventDefault(); jumpTo(act.dataset.date); }
    });

    $('resultsToggle').addEventListener('click', () => {
      state.showAllResults = !state.showAllResults; renderResults();
    });

    $('syncBtn').addEventListener('click', () => loadData(true));

    // Mobile menu
    const menu = $('menuSheet'), menuBtn = $('menuBtn');
    const closeMenu = () => { menu.hidden = true; menuBtn.setAttribute('aria-expanded', 'false'); };
    menuBtn.addEventListener('click', e => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      menuBtn.setAttribute('aria-expanded', String(!menu.hidden));
    });
    menu.addEventListener('click', e => { if (e.target.closest('a')) closeMenu(); });
    document.addEventListener('click', e => { if (!menu.hidden && !e.target.closest('#menuSheet')) closeMenu(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenu(); });

    // Highlight Schedule / Results in the nav while scrolling
    const updateActive = () => {
      const r = $('results').getBoundingClientRect();
      const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 4;
      const active = (r.top < window.innerHeight * 0.45 || nearBottom) ? 'results' : 'schedule';
      document.querySelectorAll('[data-link]').forEach(a => a.classList.toggle('is-active', a.dataset.link === active));
    };
    window.addEventListener('scroll', updateActive, { passive: true });
    updateActive();

    // Keep "updated X min ago" and Awaiting/Upcoming status fresh
    setInterval(() => { if (state.data) render(); }, 60 * 1000);

    // Check for new data while the page is open (the sync publishes every ~30 min).
    setInterval(() => { if (!document.hidden) loadData(false, true); }, REFRESH_MS);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && Date.now() - state.lastFetch > 2 * 60 * 1000) loadData(false, true);
    });
  }

  const REFRESH_MS = 5 * 60 * 1000;

  function clearFilters() {
    state.filters = { sport: 'all', gender: 'all', level: 'all', ha: 'all' };
    render();
  }

  /**
   * manual = user clicked the sync chip; background = periodic refresh.
   * If a refresh fails we keep showing the data we already have.
   */
  async function loadData(manual, background) {
    const btn = $('syncBtn');
    if (!background) btn.classList.add('is-loading');
    state.lastFetch = Date.now();
    try {
      const fresh = await window.BXSData.load({ bustCache: !!(manual || background) });
      const changed = !state.data || JSON.stringify(fresh) !== JSON.stringify(state.data);
      state.data = fresh;
      state.error = null;
      state.refreshFailed = false;
      if (changed || manual || !background) render();
      else renderSync();
    } catch (err) {
      console.error(err);
      if (state.data) state.refreshFailed = true; else state.error = err;
      render();
    } finally {
      btn.classList.remove('is-loading');
    }
  }

  bind();
  render();
  loadData(false);
})();
