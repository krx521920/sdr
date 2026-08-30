const CHANNELS = [
  'email',
  'whatsapp',
  'linkedin',
  'feishu',
  'apollo',
  'facebook',
  'wechat',
  'wecom'
];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const object = (value) =>
  value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const text = (value, max = 120) => (typeof value === 'string' ? value.trim().slice(0, max) : '');
const integer = (value, max = 10_000_000) =>
  Number.isInteger(value) && value >= 0 ? Math.min(value, max) : 0;

export function normalizeChannelSafety(raw) {
  const value = object(raw);
  const organization = object(value.organization);
  const channels = Array.isArray(value.channels) ? value.channels : [];
  const targets = Array.isArray(value.test_targets) ? value.test_targets : [];
  const approvals = Array.isArray(value.approvals) ? value.approvals : [];
  const unknown = Array.isArray(value.unknown_requests) ? value.unknown_requests : [];
  return {
    environmentEnabled: value.environment_enabled === true,
    organization: {
      enabled: organization.enabled === true,
      dailyLimit: integer(organization.daily_limit),
      reservedUnits: integer(organization.reserved_units),
      consumedUnits: integer(organization.consumed_units),
      revision: integer(organization.revision, Number.MAX_SAFE_INTEGER)
    },
    channels: channels
      .map((item) => {
        const row = object(item);
        const channel = CHANNELS.includes(row.channel) ? row.channel : '';
        return {
          channel,
          implemented: row.implemented === true && !['wechat', 'wecom'].includes(channel),
          enabled: row.enabled === true && !['wechat', 'wecom'].includes(channel),
          testMode: row.test_mode === true,
          dailyLimit: integer(row.daily_limit, 1_000_000),
          perExecutionLimit: integer(row.per_execution_limit, 1_000_000),
          reservedUnits: integer(row.reserved_units, 1_000_000),
          consumedUnits: integer(row.consumed_units, 1_000_000),
          revision: integer(row.revision, Number.MAX_SAFE_INTEGER)
        };
      })
      .filter((item) => item.channel),
    testTargets: targets
      .map((item) => {
        const row = object(item);
        return {
          id: UUID.test(String(row.id || '')) ? String(row.id) : '',
          channel: CHANNELS.includes(row.channel) ? row.channel : '',
          safeLabel: text(row.safe_label),
          active: row.active === true
        };
      })
      .filter((item) => item.id && item.channel && item.safeLabel),
    approvals: approvals
      .map((item) => {
        const row = object(item);
        return {
          id: UUID.test(String(row.id || '')) ? String(row.id) : '',
          channel: CHANNELS.includes(row.channel) ? row.channel : '',
          action: text(row.action, 64),
          safeLabel: text(row.safe_label),
          units: integer(row.units, 1_000_000),
          expiresAt: text(row.expires_at, 40)
        };
      })
      .filter((item) => item.id && item.channel),
    unknownRequests: unknown
      .map((item) => {
        const row = object(item);
        return {
          id: UUID.test(String(row.id || '')) ? String(row.id) : '',
          channel: CHANNELS.includes(row.channel) ? row.channel : '',
          action: text(row.action, 64),
          units: integer(row.units, 1_000_000),
          status: row.status === 'unknown' ? 'unknown' : '',
          sendingAt: text(row.sending_at, 40),
          unknownAt: text(row.unknown_at, 40)
        };
      })
      .filter((item) => item.id && item.channel && item.status === 'unknown')
  };
}

export function safeChannel(value) {
  return CHANNELS.includes(value) && !['wechat', 'wecom'].includes(value) ? value : '';
}

export function validUuid(value) {
  return UUID.test(String(value || ''));
}
