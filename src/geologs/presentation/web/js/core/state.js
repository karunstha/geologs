// Shared client state for every view.
export const state = {
  authDismissed: false,
  // The signed-in user ({id, email, name, role}), from /api/me; their account page, when opened.
  user: null,
  account: null,
  points: [],
  timeline: [],
  places: [],
  visitedPlaces: [],
  reminders: [],
  reminderHistory: [],
  events: [],
  day: null,
  lastEventId: 0,
  // The newest tracker notice seen; null until the first poll, so old notices are not shown again.
  lastTrackerNotice: null,
  insights: null,
  settings: null,
  followToday: true,
  focusPlace: null,
  coverage: null,
  current: null,
  sampleView: "timeline",
  rawPage: 0,
  rawPageSize: 50,
  rawTotal: 0,
  // Sample ids ticked in the Samples table; only those shown stay selected.
  selectedSamples: new Set(),
  // Device tokens for sending locations ({ tokens, env_token }), from /api/ingest-tokens.
  ingestTokens: null,
  selected: null,
  fitKey: null,
  pendingStayId: null,
};
