import os
import re
import json
from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from dotenv import load_dotenv
import openai
import concurrent.futures

# ---------------------- THREAD POOL ----------------------
executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# ---------------------- LOAD ENV ----------------------
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
APPROVER_ID = os.getenv("APPROVER_ID")
openai.api_key = os.getenv("OPENAI_API_KEY")

# ---------------------- INIT --------------------------
bolt_app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
flask_app = Flask(__name__)
handler = SlackRequestHandler(bolt_app)

# ---------------------- HELPERS -----------------------
def clean_text(s):
    if not s:
        return ""
    return (
        s.replace("\\n", " ")
        .replace("\\t", " ")
        .replace("\\", "")
        .replace('"', "")
        .replace("'", "")
        .strip()
    )

def call_openai_summary(raw_message):
    cleaned = clean_text(raw_message)
    if not cleaned or len(cleaned) < 10:
        return "Could not extract a valid Salesforce error message."

    prompt = f"""
    Summarize this Salesforce error message for a support engineer.
    Explain in plain English what went wrong and how the user can fix it.

    Error message:
    {cleaned}
    """

    try:
        completion = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"Could not summarize error: {e}"

def parse_error_blocks(full_text):
    """
    Splits a giant log message into individual error blocks.
    Each block starts with 'URL:' and continues until the next 'URL:'.
    Returns a list of parsed error dicts.
    """
    blocks = full_text.split("URL:")
    parsed_errors = []

    for block in blocks[1:]:  # skip the first part before first URL
        text = "URL:" + block.strip()

        # email
        email = re.search(r"[\w\.-]+@[\w\.-]+", text)
        email = email.group(0) if email else None

        # error code
        code = re.search(r"Error code\s*=\s*(\d+)", text)
        code = code.group(1) if code else None

        # message
        message_match = re.search(r"'message':\s*['\"](.+?)['\"]", text)
        message = message_match.group(1).strip() if message_match else None

        # Only accept 400/409 errors
        if email and code in ["400", "409"] and message:
            parsed_errors.append({
                "email": email,
                "code": code,
                "message": message
            })

    return parsed_errors

def draft_fix_message(email, message):
    msg_lower = message.lower()
    if "already exists" in msg_lower:
        suggestion = "Please check Salesforce — a record with this data already exists."
    elif "must be" in msg_lower or "validation" in msg_lower:
        suggestion = f"Please correct this field: {message}"
    elif "not found" in msg_lower:
        suggestion = "The requested Salesforce record doesn’t exist."
    else:
        suggestion = call_openai_summary(message)

    return f"Hi {email}, {suggestion}"

# ---------------------- SLACK HANDLERS ----------------------
@bolt_app.event("message")
def handle_message_events(body, say, logger):
    event = body.get("event", {})
    text = event.get("text", "")
    user = event.get("user")

    if not text or event.get("bot_id"):
        return

    if text.lower().strip() in ["hi", "hello", "hey"]:
        say(f"Hey <@{user}> 👋 I'm alive and connected!")
        return

    # Parse MULTIPLE errors in one Slack message
    parsed_errors = parse_error_blocks(text)
    if not parsed_errors:
        return

    print("✅ Found errors:", parsed_errors)

    approver_ids = [APPROVER_ID, os.getenv("SECOND_APPROVER_ID")]

    # Send each error as a separate approval block
    for parsed in parsed_errors:
        draft = draft_fix_message(parsed["email"], parsed["message"])

        for approver in approver_ids:
            if approver:
                bolt_app.client.chat_postMessage(
                    channel=approver,
                    text=f"*Detected Salesforce Error*\nEmail: {parsed['email']}\nCode: {parsed['code']}\nError: {parsed['message']}\n\n*Draft Message:*\n{draft}",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Detected Salesforce Error*\nEmail: {parsed['email']}\nCode: {parsed['code']}\nError: {parsed['message']}\n\n*Draft Message:*\n{draft}",
                            },
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "✅ Approve"},
                                    "style": "primary",
                                    "value": json.dumps(parsed | {"draft": draft}),
                                    "action_id": "approve_fix",
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "✏️ Edit"},
                                    "value": json.dumps(parsed | {"draft": draft}),
                                    "action_id": "edit_fix",
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "🚫 Reject"},
                                    "style": "danger",
                                    "value": "reject",
                                    "action_id": "reject_fix",
                                },
                            ],
                        },
                    ],
                )

# ---------- APPROVE ----------
@bolt_app.action("approve_fix")
def approve_fix(ack, body, logger):
    ack(response_action="clear")
    def handle_action(app_instance):
        try:
            data = json.loads(body["actions"][0]["value"])
            email = data["email"]
            draft = data["draft"]

            try:
                res = app_instance.client.users_lookupByEmail(email=email)
                user_id = res["user"]["id"]
                app_instance.client.chat_postMessage(channel=user_id, text=draft)
                app_instance.client.chat_postMessage(
                    channel=APPROVER_ID,
                    text=f"✅ Message sent to Slack user ({email})"
                )
            except Exception as e:
                app_instance.client.chat_postMessage(
                    channel=APPROVER_ID,
                    text=f"⚠️ Could not find Slack user for {email}. Please send manually.\nError: {e}"
                )
        except Exception as e:
            logger.error(f"Error handling approve_fix: {e}")
    executor.submit(handle_action, bolt_app)

# ---------- EDIT ----------
@bolt_app.action("edit_fix")
def edit_fix(ack, body):
    ack()
    data = json.loads(body["actions"][0]["value"])
    draft = data["draft"]

    # open a modal with prefilled draft
    bolt_app.client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_edit",
            "title": {"type": "plain_text", "text": "Edit Draft Message"},
            "submit": {"type": "plain_text", "text": "Send"},
            "private_metadata": json.dumps(data),
            "blocks": [
                {
                    "type": "input",
                    "block_id": "edit_block",
                    "label": {"type": "plain_text", "text": "Edit your message"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "edited_text",
                        "multiline": True,
                        "initial_value": draft,
                    },
                }
            ],
        },
    )

@bolt_app.view("submit_edit")
def handle_edit_submission(ack, body, logger):
    ack()
    private_data = json.loads(body["view"]["private_metadata"])
    edited_text = body["view"]["state"]["values"]["edit_block"]["edited_text"]["value"]
    email = private_data["email"]

    # Try sending to Slack user or notify approver
    try:
        res = bolt_app.client.users_lookupByEmail(email=email)
        user_id = res["user"]["id"]
        bolt_app.client.chat_postMessage(channel=user_id, text=edited_text)
        bolt_app.client.chat_postMessage(
            channel=APPROVER_ID,
            text=f"✏️ Edited message sent to Slack user ({email})"
        )
    except Exception as e:
        bolt_app.client.chat_postMessage(
            channel=APPROVER_ID,
            text=f"⚠️ Could not find Slack user for {email}. Please send manually.\nError: {e}"
        )

# ---------- REJECT ----------
@bolt_app.action("reject_fix")
def reject_fix(ack):
    ack()
    bolt_app.client.chat_postMessage(channel=APPROVER_ID, text="🚫 Message rejected. No action taken.")

# ---------------------- FLASK ROUTE ----------------------
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.get_json(silent=True) or {}
    if data.get("type") == "url_verification":
        # respond to Slack's verification challenge
        return {"challenge": data["challenge"]}, 200
    # otherwise, let Bolt handle the real events
    return handler.handle(request)

# ---------------------- RUN ----------------------
if __name__ == "__main__":
    print("⚡ Leadbeam Error Agent running...")
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)