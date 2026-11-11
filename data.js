/* =========================================================
   BXSCI Athletics — data layer
   ---------------------------------------------------------
   This file ONLY loads and cleans up schedule data. It knows
   nothing about the page design or the calendar.

   Today it reads the local `games.json` file. Later, an
   automated sync script (for example a scheduled GitHub
   Action) can regenerate `games.json` in the same format and
   nothing else in the site needs to change.

   Exposes: window.BXSData.load()  -> Promise<{ meta, games }>
   ========================================================= */

window.BXSData = (function () {
  'use strict';

  const DATA_URL = 'games.json';
  // Small "still checking PSAL" file written by the sync after every successful
  // check (see vercel.json rewrite in the workflow). Lets "Last updated" stay
  // truthful when a check found no changes and the site was not redeployed.
  const HEARTBEAT_URL = 'sync-status.json';

  const STATUSES = ['scheduled', 'final', 'postponed', 'canceled'];
  const HOME_AWAY = ['home', 'away', 'neutral'];
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;      // "2026-11-09"
  const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/; // "16:00" (24-hour, Eastern Time)

  /** Fetch games.json and return normalized { meta, games }. */
  async function load(options = {}) {
    const url = options.bustCache ? `${DATA_URL}?t=${Date.now()}` : DATA_URL;
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`Could not load ${DATA_URL} (HTTP ${res.status})`);
    const raw = await res.json();
    const data = normalize(raw);
    await applyHeartbeat(data, raw);
    return data;
  }

  /** Use the newer "checked PSAL at" time, but ONLY if that check confirmed
   *  exactly the data this page is showing (same dataHash). Any problem → ignore. */
  async function applyHeartbeat(data, raw) {
    const hash = raw && raw.meta && raw.meta.dataHash;
    if (!hash || data.meta.source !== 'psal') return;
    try {
      const res = await fetch(HEARTBEAT_URL, { cache: 'no-store' });
      if (!res.ok) return;
      const hb = await res.json();
      if (!hb || hb.status !== 'ok' || hb.dataHash !== hash) return;
      const t = Date.parse(hb.checkedAt);
      if (isNaN(t) || t > Date.now() + 5 * 60e3) return;           // ignore clock nonsense
      if (!data.meta.lastSynced || t > Date.parse(data.meta.lastSynced)) {
        data.meta.lastSynced = hb.checkedAt;
      }
    } catch (e) { /* heartbeat is optional */ }
  }

  /** Validate + clean the raw JSON so the UI can trust every field. */
  function normalize(raw) {
    const meta = raw && typeof raw.meta === 'object' ? raw.meta : {};
    const list = Array.isArray(raw && raw.games) ? raw.games : [];

    const games = list
      .map(normalizeGame)
      .filter(Boolean)
      .sort(compareGames);

    const lastSynced = typeof meta.lastSynced === 'string' && !isNaN(Date.parse(meta.lastSynced))
      ? meta.lastSynced
      : null;

    return {
      meta: {
        schemaVersion: meta.schemaVersion || 1,
        school: meta.school || 'Bronx High School of Science',
        source: meta.source || 'unknown',          // "sample" | "psal" | "manual"
        isSample: meta.source === 'sample',
        lastSynced,                                  // ISO timestamp with offset, or null
        timezone: 'America/New_York',
        seasons: Array.isArray(meta.seasons)
          ? meta.seasons.filter(s => s && DATE_RE.test(s.start) && DATE_RE.test(s.end))
          : [],
        dayNotes: meta.dayNotes && typeof meta.dayNotes === 'object' ? meta.dayNotes : {}
      },
      games
    };
  }

  function normalizeGame(g, index) {
    if (!g || !DATE_RE.test(g.date)) {
      console.warn('[BXSData] Skipping game with missing/invalid date:', g);
      return null;
    }
    const status = STATUSES.includes(String(g.status).toLowerCase())
      ? String(g.status).toLowerCase()
      : 'scheduled';

    const hasScore = g.score
      && Number.isFinite(Number(g.score.bxsci))
      && Number.isFinite(Number(g.score.opponent))
      && g.score.bxsci !== null && g.score.opponent !== null;

    const homeAway = HOME_AWAY.includes(String(g.homeAway).toLowerCase())
      ? String(g.homeAway).toLowerCase()
      : 'neutral';

    return {
      id: String(g.id || `game-${index}`),
      date: g.date,                                       // "YYYY-MM-DD" (Eastern Time)
      time: TIME_RE.test(g.time || '') ? g.time : null,   // "HH:MM" 24h, or null = TBA
      sport: clean(g.sport) || 'Other',
      gender: normalizeGender(g.gender),                  // "Boys" | "Girls" | "Coed"
      level: normalizeLevel(g.level),                     // "Varsity" | "JV"
      opponent: clean(g.opponent) || null,                // null for meets/championships
      event: clean(g.event) || null,                      // e.g. "Bronx Borough Championships"
      round: clean(g.round) || null,                      // e.g. "Playoffs · Round 2"
      homeAway,
      location: clean(g.location) || 'Location TBA',
      status,
      score: hasScore ? { bxsci: Number(g.score.bxsci), opponent: Number(g.score.opponent) } : null,
      resultText: clean(g.resultText) || null,            // e.g. "4th of 18" for meets
      statusNote: clean(g.statusNote) || null             // e.g. "New date TBA"
    };
  }

  function normalizeGender(v) {
    const s = String(v || '').toLowerCase();
    if (s.startsWith('b')) return 'Boys';
    if (s.startsWith('g')) return 'Girls';
    return 'Coed';
  }

  function normalizeLevel(v) {
    const s = String(v || '').toLowerCase().replace(/[\s.]/g, '');
    return s === 'jv' || s === 'juniorvarsity' ? 'JV' : 'Varsity';
  }

  function clean(v) {
    return typeof v === 'string' ? v.trim() : '';
  }

  // Chronological: date, then time (TBA last), then sport.
  function compareGames(a, b) {
    const ka = a.date + ' ' + (a.time || '99:99');
    const kb = b.date + ' ' + (b.time || '99:99');
    if (ka !== kb) return ka < kb ? -1 : 1;
    return a.sport.localeCompare(b.sport);
  }

  return { load, normalize };
})();
