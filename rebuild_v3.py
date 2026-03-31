import json, urllib.request, urllib.error, ssl, os

def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())
_load_env()

N8N_BASE_URL  = "http://localhost:5678"
N8N_API_KEY   = os.environ["N8N_API_KEY"]
WF_ID         = "wNuepNHNsdIwXFFY"
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]
FAL_API_KEY   = os.environ["FAL_API_KEY"]
TG_BOT_TOKEN  = os.environ["TG_BOT_TOKEN"]
WEBHOOK_PATH  = "brand-automation-tg-trigger"

TG_MSG_URL       = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
TG_PHOTO_URL     = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
TG_ANSWER_CB_URL = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery"
FAL_URL          = "https://fal.run/fal-ai/flux/schnell"

def n8n(method, path, body=None):
    url  = f"{N8N_BASE_URL}/api/v1{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method,
                                  headers={"X-N8N-API-KEY": N8N_API_KEY,
                                           "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise Exception(f"HTTP {e.code}: {e.read().decode()}")

# ─────────────────────────────────────────────────────────────────
#  System prompt
# ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a content writer for a full-stack engineer and system architect based in Uzbekistan "
    "who builds real business systems \u2014 not tutorials, not demos \u2014 actual production platforms. "
    "TONE: Direct, no fluff, no corporate speak, thinks like a builder. "
    "AUDIENCE: Developers moving into architecture, founders who build their own tech, engineers curious about AI automation. "
    "PLATFORM: Instagram caption max 2200 chars. "
    "FORMAT: First line is scroll-stopping hook (no I as first word). Short paragraphs max 3 lines. "
    "End with question or CTA. 5-8 hashtags at bottom. "
    "OUTPUT: Return ONLY valid JSON, no markdown, no explanation: "
    '{ "caption": "", "image_prompt": "" }'
)

# ─────────────────────────────────────────────────────────────────
#  Step numbering:
#   step=1  new session  → generate caption
#   step=2  caption sent → waiting for approve/redo/cancel
#   step=3  approved     → waiting for image description
#   step=4  image sent   → waiting for approve/regenerate/enhance/reprompt/cancel
#   step=0  session just ended (approved/cancelled) — absorb next msg, then reset
# ─────────────────────────────────────────────────────────────────

# Handles both regular messages and inline button callback_query
GET_SESSION_STATE_JS = """\
const staticData = $getWorkflowStaticData('global');
const body = $input.first().json.body;
const chatId = String(
  body.callback_query?.message?.chat?.id ||
  body.message?.chat?.id || ''
);
const messageText = (
  body.callback_query?.data ||
  body.message?.text || ''
).trim();
const callbackQueryId = body.callback_query?.id || '';
const ml = messageText.toLowerCase();

// Safety: get session with step=1 as default
let session = staticData[chatId] || { step: 1 };
if (session.step === undefined || session.step === null) {
  session.step = 1;
  staticData[chatId] = session;
}

// step=0: session just ended (approved/cancelled)
// Absorb this message, clear state — NEXT message is a clean new session
if (session.step === 0) {
  delete staticData[chatId];
  return [{ json: { ...body, chatId, messageText, callbackQueryId, currentStep: 'noop', session } }];
}

// Control commands with no active session (step=1) — route to noop
const controlCmds = ['/approve', '/redo', '/regenerate', '/enhance', '/reprompt', '/cancel',
  '\u2705', '\u274c', '\u270f\ufe0f', '\ud83d\udd04'];
if (session.step === 1 && controlCmds.includes(ml)) {
  return [{ json: { ...body, chatId, messageText, callbackQueryId, currentStep: 'noop', session } }];
}

// For step=2 and step=4, invalid inputs are kept at their step
// and handled as action='invalid' in the decision nodes (sends reminder)

return [{
  json: {
    ...body,
    chatId,
    messageText,
    callbackQueryId,
    currentStep: String(session.step),
    session
  }
}];
"""

# Step 1: Call Claude → parse → save → send caption preview → set step=2
PARSE_CAPTION_JS = """\
const raw = $input.first().json.content[0].text;
const cleaned = raw.replace(/```json\\n?/g, '').replace(/```\\n?/g, '').trim();
const parsed = JSON.parse(cleaned);
const chatId = $('Get Session State').first().json.chatId;
const contentIdea = $('Get Session State').first().json.messageText;
const staticData = $getWorkflowStaticData('global');
staticData[chatId] = {
  step: 2,
  caption: parsed.caption,
  image_prompt: parsed.image_prompt,
  content_idea: contentIdea,
  image_description: '',
  image_url: ''
};
return [{ json: { chatId, caption: parsed.caption } }];
"""

# Step 2: route approve / redo / cancel; default to 'invalid' for anything else
PROCESS_CAPTION_DECISION_JS = """\
const chatId = $input.first().json.chatId;
const msg = $input.first().json.messageText;
const session = $input.first().json.session;
const staticData = $getWorkflowStaticData('global');

const inp = msg.trim().toLowerCase();
let action = 'invalid';
if (['\u2705', '/approve', 'approve', 'yes', 'ok', 'looks good', 'good'].includes(inp)) action = 'approve';
else if (['\u270f\ufe0f', '/redo', 'redo', 'regenerate', 'again', 'retry'].includes(inp)) action = 'regenerate';
else if (['\u274c', '/cancel', 'cancel', 'no', 'stop'].includes(inp)) action = 'cancel';

if (action === 'approve') {
  staticData[chatId] = { ...session, step: 3 };
} else if (action === 'cancel') {
  delete staticData[chatId];
  staticData[chatId] = { step: 0 };
}
// regenerate: keep step=2, re-generate with same content_idea
// invalid: keep step=2, decision node routes to reminder

return [{ json: { chatId, action, contentIdea: session.content_idea || '' } }];
"""

# Step 2 regenerate: re-parse and save back to step=2
PARSE_CAPTION_REGEN_JS = """\
const raw = $input.first().json.content[0].text;
const cleaned = raw.replace(/```json\\n?/g, '').replace(/```\\n?/g, '').trim();
const parsed = JSON.parse(cleaned);
const chatId = $('Process Caption Decision').first().json.chatId;
const staticData = $getWorkflowStaticData('global');
const existing = staticData[chatId] || {};
staticData[chatId] = { ...existing, step: 2, caption: parsed.caption, image_prompt: parsed.image_prompt };
return [{ json: { chatId, caption: parsed.caption } }];
"""

# Step 3: receive image description → FAL → save → set step=4
STORE_IMAGE_JS = """\
const imageUrl = $input.first().json.images[0].url;
const chatId = $('Get Session State').first().json.chatId;
const imageDescription = $('Get Session State').first().json.messageText;
const staticData = $getWorkflowStaticData('global');
const state = staticData[chatId] || {};
staticData[chatId] = { ...state, step: 4, image_url: imageUrl, image_description: imageDescription };
return [{ json: { chatId, imageUrl, chosenCaption: state.caption || '' } }];
"""

# Step 4: route approve / regenerate / enhance / reprompt / cancel; default to 'invalid'
PROCESS_DECISION_JS = """\
const chatId = $input.first().json.chatId;
const msg = $input.first().json.messageText;
const session = $input.first().json.session;
const staticData = $getWorkflowStaticData('global');

const inp = msg.trim().toLowerCase();
let action = 'invalid';
let imageDescription = session.image_description || '';

if (['\u2705', '/approve', 'approve', 'yes', 'ok', 'looks good', 'good', 'post'].includes(inp)) {
  action = 'approve';
} else if (['\ud83d\udd04', '/regenerate', 'regenerate', 'redo', 'again', 'retry'].includes(inp)) {
  action = 'regenerate';
} else if (inp === '/enhance') {
  action = 'regenerate';
  imageDescription = imageDescription + ', ultra high quality, 8k resolution, photorealistic, highly detailed';
} else if (inp === '/reprompt') {
  action = 'reprompt';
  staticData[chatId] = { ...session, step: 3 };
} else if (['\u274c', '/cancel', 'cancel', 'no', 'stop'].includes(inp)) {
  action = 'cancel';
}

if (action === 'approve' || action === 'cancel') {
  delete staticData[chatId];
  staticData[chatId] = { step: 0 };
}
// regenerate: keep step=4
// reprompt: step already set to 3 above
// invalid: keep step=4, decision node routes to reminder

return [{ json: {
  chatId,
  action,
  chosenCaption: session.caption || '',
  imageDescription,
  imageUrl: session.image_url || ''
} }];
"""

STORE_REGEN_JS = """\
const imageUrl = $input.first().json.images[0].url;
const chatId = $('Process Decision').first().json.chatId;
const chosenCaption = $('Process Decision').first().json.chosenCaption;
const imageDescription = $('Process Decision').first().json.imageDescription;
const staticData = $getWorkflowStaticData('global');
staticData[chatId] = {
  ...(staticData[chatId] || {}),
  step: 4,
  image_url: imageUrl,
  image_description: imageDescription,
  caption: chosenCaption
};
return [{ json: { chatId, imageUrl, chosenCaption } }];
"""

# Builds Telegram sendMessage payload with inline keyboard for caption approval.
# Runs as a Code node before Send Caption so the HTTP node can use
# ={{ JSON.stringify($json) }} — avoids the }} expression-delimiter collision.
BUILD_CAPTION_PAYLOAD_JS = """\
const chatId = $input.first().json.chatId;
const caption = $input.first().json.caption;
return [{ json: {
  chat_id: chatId,
  text: "Here is your caption:\\n\\n" + caption,
  reply_markup: {
    inline_keyboard: [[
      { text: "\\u2705 Approve",    callback_data: "/approve" },
      { text: "\\u270f\\ufe0f Redo", callback_data: "/redo" },
      { text: "\\u274c Cancel",     callback_data: "/cancel" }
    ]]
  }
} }];
"""

# Builds Telegram sendPhoto payload with inline keyboard for image approval.
BUILD_IMAGE_PAYLOAD_JS = """\
const chatId = $input.first().json.chatId;
const imageUrl = $input.first().json.imageUrl;
const caption = ($input.first().json.chosenCaption || "").substring(0, 800);
return [{ json: {
  chat_id: chatId,
  photo: imageUrl,
  caption: "Here is your preview.\\n\\nCaption:\\n" + caption,
  reply_markup: {
    inline_keyboard: [
      [
        { text: "\\u2705 Approve",        callback_data: "/approve" },
        { text: "\\ud83d\\udd04 Regenerate", callback_data: "/regenerate" }
      ],
      [
        { text: "\\u2728 Enhance",         callback_data: "/enhance" },
        { text: "\\u270d\\ufe0f Reprompt",  callback_data: "/reprompt" },
        { text: "\\u274c Cancel",           callback_data: "/cancel" }
      ]
    ]
  }
} }];
"""

# Builds Telegram answerCallbackQuery payload only when current update is a callback.
BUILD_CALLBACK_PAYLOAD_JS = """\
const callbackQueryId = $input.first().json.callbackQueryId || '';
if (!callbackQueryId) {
  return [];
}
return [{ json: { callback_query_id: callbackQueryId } }];
"""

# ─────────────────────────────────────────────────────────────────
#  HTTP body expressions
# ─────────────────────────────────────────────────────────────────

def claude_body(content_expr):
    return (
        '={{ JSON.stringify({'
        '"model": "claude-haiku-4-5-20251001",'
        '"max_tokens": 500,'
        '"system": ' + json.dumps(SYSTEM_PROMPT) + ','
        '"messages": [{"role": "user", "content": ' + content_expr + '}]'
        '}) }}'
    )

CLAUDE_BODY       = claude_body("$json.messageText")
CLAUDE_REGEN_BODY = claude_body("$json.contentIdea")
TG_RAW_BODY       = '={{ JSON.stringify($json) }}'

SEND_IMG_REQ_BODY = (
    '={{ JSON.stringify({'
    '"chat_id": $json.chatId,'
    r'"text": "Got it!\n\nNow describe the image you want.\n\nExample: Dark minimal desk, ultrawide monitor showing code, blue accent lighting, no people, cinematic, photorealistic"'
    '}) }}'
)

FAL_BODY_STEP3 = (
    '={{ JSON.stringify({"prompt": $json.messageText, "image_size": "square_hd", "num_images": 1}) }}'
)

FAL_BODY_REGEN = (
    '={{ JSON.stringify({"prompt": $json.imageDescription, "image_size": "square_hd", "num_images": 1}) }}'
)

SEND_POSTED_BODY = (
    '={{ JSON.stringify({'
    '"chat_id": $json.chatId,'
    r'"text": "Posted! \u2705\n\nYour content is approved and ready.\n\nSend a new content idea whenever you\u2019re ready."'
    '}) }}'
)

SEND_CANCELLED_BODY = (
    '={{ JSON.stringify({'
    '"chat_id": $json.chatId,'
    r'"text": "Cancelled. \u274c\n\nSend a new content idea to start over."'
    '}) }}'
)

ASK_PROMPT_BODY = (
    '={{ JSON.stringify({'
    '"chat_id": $json.chatId,'
    r'"text": "Send your new image description:"'
    '}) }}'
)

BUILD_NO_SESSION_PAYLOAD_JS = """\
const chatId = $input.first().json.chatId;
return [{ json: { chat_id: chatId, text: "No active session.\\n\\nSend a content idea to start." } }];
"""

REMIND_CAPTION_BODY = (
    '={{ JSON.stringify({'
    '"chat_id": $json.chatId,'
    r'"text": "Please use the buttons above, or type:\n/approve \u2014 proceed\n/redo \u2014 regenerate\n/cancel \u2014 cancel"'
    '}) }}'
)

REMIND_IMAGE_BODY = (
    '={{ JSON.stringify({'
    '"chat_id": $json.chatId,'
    r'"text": "Please use the buttons above, or type:\n/approve \u2014 post\n/regenerate \u2014 new image\n/enhance \u2014 improve quality\n/reprompt \u2014 new description\n/cancel \u2014 cancel"'
    '}) }}'
)

# ─────────────────────────────────────────────────────────────────
#  Node builders
# ─────────────────────────────────────────────────────────────────

def code_node(nid, name, pos, js):
    return {
        "id": nid, "name": name,
        "type": "n8n-nodes-base.code", "typeVersion": 2,
        "position": pos,
        "parameters": {"mode": "runOnceForAllItems", "jsCode": js}
    }

def http_node(nid, name, pos, method, url, headers, body_expr, continue_on_fail=False):
    node = {
        "id": nid, "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": pos,
        "parameters": {
            "method": method, "url": url,
            "sendHeaders": True,
            "headerParameters": {"parameters": headers},
            "sendBody": True,
            "contentType": "raw", "rawContentType": "application/json",
            "body": body_expr
        }
    }
    if continue_on_fail:
        node["continueOnFail"] = True
    return node

def if_node(nid, name, pos, field, value):
    return {
        "id": nid, "name": name,
        "type": "n8n-nodes-base.if", "typeVersion": 1,
        "position": pos,
        "parameters": {
            "conditions": {
                "string": [{"value1": f"={{{{ $json.{field} }}}}", "operation": "equal", "value2": value}]
            }
        }
    }

def switch_node(nid, name, pos, field, values):
    rules = [{"value2": str(v), "output": i} for i, v in enumerate(values)]
    return {
        "id": nid, "name": name,
        "type": "n8n-nodes-base.switch", "typeVersion": 1,
        "position": pos,
        "parameters": {
            "dataType": "string",
            "value1": f"={{{{ $json.{field} }}}}",
            "rules": {"rules": rules},
            "fallbackOutput": "none"
        }
    }

CT      = [{"name": "content-type", "value": "application/json"}]
FAL_HDR = [{"name": "Authorization", "value": f"Key {FAL_API_KEY}"}, {"name": "content-type", "value": "application/json"}]
ANT_HDR = [{"name": "x-api-key", "value": ANTHROPIC_KEY}, {"name": "anthropic-version", "value": "2023-06-01"}, {"name": "content-type", "value": "application/json"}]
ANT_URL = "https://api.anthropic.com/v1/messages"

# ─────────────────────────────────────────────────────────────────
#  Nodes
# ─────────────────────────────────────────────────────────────────

nodes = [
    # ── Core ──────────────────────────────────────────────────────
    {
        "id": "n-webhook", "name": "Telegram Webhook",
        "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [100, 400],
        "webhookId": WEBHOOK_PATH,
        "parameters": {
            "httpMethod": "POST", "path": WEBHOOK_PATH,
            "responseMode": "onReceived", "options": {}
        }
    },
    code_node("n-get-session", "Get Session State", [320, 400], GET_SESSION_STATE_JS),

    # Build callback payload only for callback_query updates, then answer spinner.
    code_node("n-build-cb", "Build Callback Payload", [540, 250], BUILD_CALLBACK_PAYLOAD_JS),
    http_node("n-answer-cb", "Answer Callback", [540, 250], "POST",
              TG_ANSWER_CB_URL, CT, TG_RAW_BODY, continue_on_fail=True),

    # Intercept noop (no session / session just ended) before the main switch
    if_node("n-is-noop", "Is Noop", [540, 400], "currentStep", "noop"),
    code_node("n-build-no-session", "Build No Session Payload", [760, 300], BUILD_NO_SESSION_PAYLOAD_JS),
    http_node("n-no-session", "No Active Session", [980, 300], "POST", TG_MSG_URL, CT, TG_RAW_BODY),

    # Routes on currentStep (outputs 0-3 only — switch limit)
    switch_node("n-route", "Route by Step", [760, 450], "currentStep", [1, 2, 3, 4]),

    # ── Step 1: new idea → generate caption ───────────────────────
    http_node("n-claude",   "Call Claude",   [980, 150], "POST", ANT_URL, ANT_HDR, CLAUDE_BODY),
    code_node("n-parse",    "Parse Caption", [1200, 150], PARSE_CAPTION_JS),
    code_node("n-build-cap", "Build Caption Payload", [1420, 150], BUILD_CAPTION_PAYLOAD_JS),
    http_node("n-send-cap", "Send Caption",  [1640, 150], "POST", TG_MSG_URL, CT, TG_RAW_BODY),

    # ── Step 2: caption decision ──────────────────────────────────
    code_node("n-cap-dec",       "Process Caption Decision", [980, 380], PROCESS_CAPTION_DECISION_JS),
    if_node("n-is-invalid-cap",  "Is Invalid Caption",       [1200, 380], "action", "invalid"),
    http_node("n-remind-cap",    "Remind Caption Commands",  [1420, 300], "POST", TG_MSG_URL, CT, REMIND_CAPTION_BODY),
    switch_node("n-route-cap",   "Route Caption Decision",   [1420, 420], "action",
                ["approve", "regenerate", "cancel"]),
    http_node("n-ask-img",       "Send Image Request",       [1640, 340], "POST", TG_MSG_URL, CT, SEND_IMG_REQ_BODY),
    http_node("n-claude-regen",  "Call Claude Regen",        [1640, 440], "POST", ANT_URL, ANT_HDR, CLAUDE_REGEN_BODY),
    code_node("n-parse-regen",   "Parse Caption Regen",      [1860, 440], PARSE_CAPTION_REGEN_JS),
    code_node("n-build-regen-cap", "Build Caption Regen Payload", [2080, 440], BUILD_CAPTION_PAYLOAD_JS),
    http_node("n-send-regen-cap","Send Caption Regen",       [2300, 440], "POST", TG_MSG_URL, CT, TG_RAW_BODY),
    http_node("n-cancel-cap",    "Send Cancelled Cap",       [1640, 520], "POST", TG_MSG_URL, CT, SEND_CANCELLED_BODY),

    # ── Step 3: image description → generate image ────────────────
    http_node("n-fal",       "Generate Image",     [980, 580], "POST", FAL_URL, FAL_HDR, FAL_BODY_STEP3),
    code_node("n-store-img", "Store Image URL",    [1200, 580], STORE_IMAGE_JS),
    code_node("n-build-prev", "Build Image Preview Payload", [1420, 580], BUILD_IMAGE_PAYLOAD_JS),
    http_node("n-send-prev", "Send Image Preview", [1640, 580], "POST", TG_PHOTO_URL, CT, TG_RAW_BODY),

    # ── Step 4: final decision ────────────────────────────────────
    code_node("n-decision",      "Process Decision",     [980, 760], PROCESS_DECISION_JS),
    if_node("n-is-invalid-dec",  "Is Invalid Decision",  [1200, 760], "action", "invalid"),
    http_node("n-remind-dec",    "Remind Image Commands",[1420, 680], "POST", TG_MSG_URL, CT, REMIND_IMAGE_BODY),
    switch_node("n-route-dec",   "Route Decision",       [1420, 800], "action",
                ["approve", "regenerate", "cancel", "reprompt"]),
    http_node("n-posted",        "Send Posted",          [1640, 700], "POST", TG_MSG_URL, CT, SEND_POSTED_BODY),
    http_node("n-fal-regen",     "Call FAL Again",       [1640, 800], "POST", FAL_URL, FAL_HDR, FAL_BODY_REGEN),
    code_node("n-store-regen",   "Store Regen URL",      [1860, 800], STORE_REGEN_JS),
    code_node("n-build-regen-prev", "Build Regen Preview Payload", [2080, 800], BUILD_IMAGE_PAYLOAD_JS),
    http_node("n-send-regen",    "Send Regen Preview",   [2300, 800], "POST", TG_PHOTO_URL, CT, TG_RAW_BODY),
    http_node("n-cancel-fin",    "Send Cancelled",       [1640, 900], "POST", TG_MSG_URL, CT, SEND_CANCELLED_BODY),
    http_node("n-ask-prompt",    "Ask New Prompt",       [1640, 980], "POST", TG_MSG_URL, CT, ASK_PROMPT_BODY),
]

# ─────────────────────────────────────────────────────────────────
#  Connections
# ─────────────────────────────────────────────────────────────────

def lnk(node):
    return [{"node": node, "type": "main", "index": 0}]

connections = {
    "Telegram Webhook": {"main": [lnk("Get Session State")]},

    # Get Session State fans out to:
    #   - Build Callback Payload -> Answer Callback
    #   - Is Noop (main flow)
    "Get Session State": {"main": [lnk("Build Callback Payload") + lnk("Is Noop")]},
    "Build Callback Payload": {"main": [lnk("Answer Callback")]},

    # Is Noop: true → No Active Session | false → Route by Step
    "Is Noop": {"main": [
        lnk("Build No Session Payload"),  # output 0 (true)
        lnk("Route by Step"),             # output 1 (false)
    ]},
    "Build No Session Payload": {"main": [lnk("No Active Session")]},

    # Route by Step: outputs 0-3 only (switch limit)
    "Route by Step": {"main": [
        lnk("Call Claude"),              # 0 → step=1
        lnk("Process Caption Decision"), # 1 → step=2
        lnk("Generate Image"),           # 2 → step=3
        lnk("Process Decision"),         # 3 → step=4
    ]},

    # Step 1
    "Call Claude":           {"main": [lnk("Parse Caption")]},
    "Parse Caption":         {"main": [lnk("Build Caption Payload")]},
    "Build Caption Payload": {"main": [lnk("Send Caption")]},

    # Step 2
    "Process Caption Decision": {"main": [lnk("Is Invalid Caption")]},
    "Is Invalid Caption": {"main": [
        lnk("Remind Caption Commands"),  # true  → invalid input
        lnk("Route Caption Decision"),   # false → valid action
    ]},
    "Route Caption Decision": {"main": [
        lnk("Send Image Request"),  # 0 approve
        lnk("Call Claude Regen"),   # 1 regenerate
        lnk("Send Cancelled Cap"),  # 2 cancel
    ]},
    "Call Claude Regen":               {"main": [lnk("Parse Caption Regen")]},
    "Parse Caption Regen":             {"main": [lnk("Build Caption Regen Payload")]},
    "Build Caption Regen Payload":     {"main": [lnk("Send Caption Regen")]},

    # Step 3
    "Generate Image":              {"main": [lnk("Store Image URL")]},
    "Store Image URL":             {"main": [lnk("Build Image Preview Payload")]},
    "Build Image Preview Payload": {"main": [lnk("Send Image Preview")]},

    # Step 4
    "Process Decision": {"main": [lnk("Is Invalid Decision")]},
    "Is Invalid Decision": {"main": [
        lnk("Remind Image Commands"),  # true  → invalid input
        lnk("Route Decision"),         # false → valid action
    ]},
    "Route Decision": {"main": [
        lnk("Send Posted"),    # 0 approve
        lnk("Call FAL Again"), # 1 regenerate / enhance
        lnk("Send Cancelled"), # 2 cancel
        lnk("Ask New Prompt"), # 3 reprompt (step already set to 3 in Process Decision)
    ]},
    "Call FAL Again":               {"main": [lnk("Store Regen URL")]},
    "Store Regen URL":              {"main": [lnk("Build Regen Preview Payload")]},
    "Build Regen Preview Payload":  {"main": [lnk("Send Regen Preview")]},
}

# ─────────────────────────────────────────────────────────────────
#  Deploy
# ─────────────────────────────────────────────────────────────────

print("Fetching workflow...")
wf = n8n("GET", f"/workflows/{WF_ID}")

print(f"Updating ({len(nodes)} nodes)...")
result = n8n("PUT", f"/workflows/{WF_ID}", {
    "name": wf["name"],
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"}
})
print("Nodes:", [n["name"] for n in result["nodes"]])

activated = n8n("POST", f"/workflows/{WF_ID}/activate")
print(f"Active: {activated.get('active')}")
print(f"ID:     {activated.get('id')}")

# ── Register Telegram webhook with callback_query support ─────────
print("\nUpdating Telegram webhook (adding callback_query)...")
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

wh_payload = json.dumps({
    "url": f"https://unpositively-sceptral-charley.ngrok-free.dev/webhook/{WEBHOOK_PATH}",
    "allowed_updates": ["message", "callback_query"]
}).encode()
wh_req = urllib.request.Request(
    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook",
    data=wh_payload, method="POST",
    headers={"Content-Type": "application/json"}
)
try:
    with urllib.request.urlopen(wh_req, context=ctx) as r:
        res = json.loads(r.read())
        print(f"  Webhook: {res.get('description')}")
except urllib.error.HTTPError as e:
    print(f"  setWebhook HTTP {e.code}: {e.read().decode()}")

# ── Register bot commands ─────────────────────────────────────────
print("Setting bot commands...")
cmd_payload = json.dumps({
    "commands": [
        {"command": "approve",    "description": "Approve and proceed"},
        {"command": "redo",       "description": "Regenerate caption"},
        {"command": "regenerate", "description": "Generate new image same description"},
        {"command": "enhance",    "description": "Improve quality keep composition"},
        {"command": "reprompt",   "description": "Write new image description"},
        {"command": "cancel",     "description": "Cancel session"},
    ]
}).encode()
cmd_req = urllib.request.Request(
    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setMyCommands",
    data=cmd_payload, method="POST",
    headers={"Content-Type": "application/json"}
)
try:
    with urllib.request.urlopen(cmd_req, context=ctx) as r:
        res = json.loads(r.read())
        print(f"  Commands set: {res.get('result')}")
except urllib.error.HTTPError as e:
    print(f"  setMyCommands HTTP {e.code}: {e.read().decode()}")
