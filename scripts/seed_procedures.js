const { Client } = require('pg');

async function main() {
  const c = new Client({
    host: '192.168.1.141',
    port: 5432,
    user: 'nous',
    password: 'nous_dev_password',
    database: 'nous',
  });
  await c.connect();

  // Procedure 1: send_email
  await c.query(
    `INSERT INTO heart.procedures (agent_id, name, domain, description, goals, core_patterns, core_tools, core_concepts, implementation_notes) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
    [
      'nous-default',
      'send_email',
      'communication',
      'Send emails from nous@cognition-engines.ai. Prefer the recipient-allowlisted send_email TOOL (safe); fall back to bash + python3 smtplib only if the tool is unavailable.',
      ['Send emails on behalf of Nous to communicate with humans', 'Support plain text emails with subject, recipient, and body'],
      [
        'PREFER the send_email TOOL: it validates the recipient against an allowlist, scans for secrets, and rate-limits — call it with {to, subject, body, cc?}',
        'FALLBACK (only if the send_email tool is unavailable): use the bash tool with python3 -c and smtplib',
        'Always include subject, recipient, and body',
        'Use Gmail SMTP (smtp.gmail.com:587, STARTTLS)',
        'Read credentials from env vars: NOUS_EMAIL and NOUS_EMAIL_PASSWORD',
        'Always set From header to the NOUS_EMAIL env var value',
        'Never include API keys, passwords, or secrets in email body',
      ],
      ['send_email', 'bash'],
      ['SMTP', 'STARTTLS', 'MIMEText', 'smtplib'],
      [
        'PREFERRED: call the send_email tool — send_email(to="RECIPIENT@example.com", subject="...", body="..."). The recipient must be on NOUS_EMAIL_ALLOWLIST or the send is refused.',
        'The rest of these notes are the bash+smtplib FALLBACK, used only when the send_email tool is unavailable:',
        'bash command template: python3 -c "\nimport smtplib, os\nfrom email.mime.text import MIMEText\nmsg = MIMEText(\'YOUR_BODY_HERE\')\nmsg[\'Subject\'] = \'YOUR_SUBJECT_HERE\'\nmsg[\'From\'] = os.environ[\'NOUS_EMAIL\']\nmsg[\'To\'] = \'RECIPIENT@example.com\'\ns = smtplib.SMTP(\'smtp.gmail.com\', 587)\ns.starttls()\ns.login(os.environ[\'NOUS_EMAIL\'], os.environ[\'NOUS_EMAIL_PASSWORD\'])\ns.send_message(msg)\ns.quit()\nprint(\'Email sent successfully\')\n"',
        'Replace YOUR_BODY_HERE, YOUR_SUBJECT_HERE, and RECIPIENT@example.com with actual values',
        'Max 5 emails per hour to avoid spam flags',
        'For multiline body, use \\n in the string',
      ],
    ]
  );
  console.log('✅ Procedure: send_email');

  // Procedure 2: notify_tim
  await c.query(
    `INSERT INTO heart.procedures (agent_id, name, domain, description, goals, core_patterns, core_tools, core_concepts, implementation_notes) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
    [
      'nous-default',
      'notify_tim',
      'communication',
      'Send Tim a notification on Telegram using the Bot API via bash + curl',
      ['Alert Tim on Telegram when something important happens', 'Proactive communication for urgent or noteworthy events'],
      [
        'Use bash tool with curl to Telegram Bot API',
        'Only notify for genuinely important events - not routine task completion',
        'Keep messages concise and actionable',
        'Read bot token from TELEGRAM_BOT_TOKEN env var',
        'Read Tim chat ID from NOUS_TIM_CHAT_ID env var',
        'Use parse_mode=HTML for formatting',
      ],
      ['bash'],
      ['Telegram Bot API', 'sendMessage', 'HTTP POST'],
      [
        'bash command template: curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" -H "Content-Type: application/json" -d \'{"chat_id": "\'${NOUS_TIM_CHAT_ID}\'", "text": "YOUR_MESSAGE_HERE", "parse_mode": "HTML"}\'',
        'For HTML formatting use <b>bold</b> and <i>italic</i>',
        'Respect quiet hours: 11 PM - 8 AM EST',
        'Good reasons to notify: errors, completed long tasks Tim asked for, urgent findings',
        'Bad reasons to notify: routine status, every decision made, trivial updates',
      ],
    ]
  );
  console.log('✅ Procedure: notify_tim');

  // Procedure 3: talk_to_emerson
  await c.query(
    `INSERT INTO heart.procedures (agent_id, name, domain, description, goals, core_patterns, core_tools, core_concepts, implementation_notes) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
    [
      'nous-default',
      'talk_to_emerson',
      'communication',
      'Send a message to Emerson (AI agent) via OpenClaw A2A webhook for collaboration',
      ['Communicate with Emerson for collaboration, questions, or sharing findings', 'Agent-to-agent communication via HTTP webhook'],
      [
        'Use bash tool with curl to OpenClaw A2A webhook',
        'Include clear context in the message - Emerson has no access to your conversation',
        'Use for: asking questions, sharing findings, requesting help, coordinating work',
        'Read hook URL from NOUS_EMERSON_HOOK_URL env var',
        'Read hook token from NOUS_EMERSON_HOOK_TOKEN env var',
        'Use wakeMode now for immediate delivery',
      ],
      ['bash'],
      ['A2A webhook', 'OpenClaw hooks', 'agent-to-agent communication'],
      [
        'bash command template: curl -s -X POST "${NOUS_EMERSON_HOOK_URL}" -H "Authorization: Bearer ${NOUS_EMERSON_HOOK_TOKEN}" -H "Content-Type: application/json" -d \'{"text": "YOUR_MESSAGE_HERE", "wakeMode": "now"}\'',
        'Emerson is another AI agent running on OpenClaw - he manages the Nous repo, cognition-engines, and other projects',
        'Messages should be self-contained - include enough context for Emerson to understand and act',
        'Good uses: share research findings, ask for code review, coordinate on shared repos, ask questions about OpenClaw',
      ],
    ]
  );
  console.log('✅ Procedure: talk_to_emerson');

  // Censor 1: email_safety
  await c.query(
    `INSERT INTO heart.censors (agent_id, trigger_pattern, action, reason, severity, is_active) VALUES ($1, $2, $3, $4, $5, $6)`,
    [
      'nous-default',
      'sending email with sensitive data',
      'suppress',
      'Never include API keys, passwords, tokens, or private data in emails. Always include a clear subject line. Max 5 emails per hour.',
      'high',
      true,
    ]
  );
  console.log('✅ Censor: email_safety');

  // Censor 2: notify_restraint
  await c.query(
    `INSERT INTO heart.censors (agent_id, trigger_pattern, action, reason, severity, is_active) VALUES ($1, $2, $3, $4, $5, $6)`,
    [
      'nous-default',
      'notifying Tim unnecessarily',
      'suppress',
      'Only notify Tim for genuinely important events. Not for routine task completion or status updates unless explicitly asked. Respect quiet hours (11 PM - 8 AM EST).',
      'medium',
      true,
    ]
  );
  console.log('✅ Censor: notify_restraint');

  // Verify
  const res = await c.query('SELECT name FROM heart.procedures WHERE agent_id = $1', ['nous-default']);
  console.log(`\nTotal procedures: ${res.rowCount}`);
  res.rows.forEach(r => console.log(`  - ${r.name}`));

  const censors = await c.query('SELECT trigger_pattern FROM heart.censors WHERE agent_id = $1 AND is_active = true', ['nous-default']);
  console.log(`Total active censors: ${censors.rowCount}`);
  censors.rows.forEach(r => console.log(`  - ${r.trigger_pattern}`));

  await c.end();
}

main().catch(e => { console.error(e); process.exit(1); });
