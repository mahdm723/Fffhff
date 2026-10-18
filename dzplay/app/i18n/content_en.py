"""V6 phase 12: English versions of the content system's default texts (see app/i18n/content.py)."""

PRINCIPLE = ("{{app}} is a community for traders to share crypto news, analysis and ideas, with your data protected: "
             "no user sees your email or any private data about you, except what you choose to show yourself "
             "(name, picture, posts).")

PRIVACY = """# 1. What we collect and why
- **Your email and an encrypted password** (or your Google ID): only for signing in, account recovery, support replies and account notifications. No user ever sees them.
- **Your display name and public ID (DZ-…)**, your profile picture if you add one, and your gender if you choose it: shown to others because you chose so.
- **Age confirmation (18+)**: we don't ask for your date of birth.
- **What you post**: ideas and their images, comments and replies, likes (people can see who liked a post; dislikes are only a number, with no names).
- **Messages**: temporary; they're deleted from the server after {{message_ttl}} at most, and sooner once read ({{message_ttl_read}}). An idle conversation is deleted after {{conversation_idle}}. Random anonymous messages have been discontinued; old anonymous conversations stay read-only and are deleted after {{legacy_anon_days}} days.
- **IP addresses**: we don't store them as they are; we only keep an encrypted, irreversible fingerprint to prevent abuse and automated attempts.
- **Phone notifications** (optional): only the device token needed to deliver them; a notification never carries a name or message text.
- **Support**: the text of your ticket, sent to the support team's email with only your public ID.
- **Membership and withdrawals**: the amount, the network and the payment transaction ID (TXID), plus the wallet address you enter for a refund or a withdrawal. We don't ask for a real name or any documents. These financial records are kept for accounting purposes, even after the account is deleted (without the email).

# 2. What others see
Your display name, picture, public ID, gender icon if you chose one, and the blue star if you have it: in posts, comments, the users page and conversations. Nobody sees your email, number, location or any internal identifier.

# 3. Access by the admin team
To run the service, moderate it and protect users, the admin team can see account data (including email), posts and comments, messages while they're kept, published images, and only those chat images that have been reported. Access is protected by an admin account and two-step verification, and every view and action is recorded in an internal audit log. Messages, comments and images are scanned automatically for harmful content.

# 4. External storage provider
Images are stored in a **private Telegram channel** owned by {{app}}, which only the platform's own bot can access. Our server passes files through and only keeps a temporary copy to speed up display. Your app never connects to Telegram, and no user ever gets the file reference. A copy of published images and profile pictures goes to a private moderators' group for review.

# 5. Metadata removal and scanning
Before saving, every image is re-encoded and all its metadata is removed: GPS location, device model, date and so on. The file is checked on your device and then on the server by an automatic pornography-detection model, and anything that breaks the rules is rejected before it's saved. No automatic check can detect people's ages, so we rely on reports and human review.

# 6. Chat images
- Sending images in chats is turned off for good: the server refuses any chat image before even reading it.
- Past chat images were deleted from the server and from storage, except reported ones: they're kept as evidence for the moderation team only, for up to {{evidence_days}} days, then deleted.

# 7. Age
{{app}} is for adults only (18 and over).

# 8. Retention
- Messages and conversations: as described in section 1.
- Reports: {{report_retention}}. Security events: {{security_retention}}. Admin audit log: {{audit_retention}}.
- Temporary copies on the server: deleted after {{cache_ttl}} without use.
- Your account and what you post: as long as your account exists.

# 9. Deleting your account
From **My account → Delete my account**. Deleted right away: the account and its sessions, the profile, name and picture, your ideas with their images, comments and reactions, your comments and reactions, conversations and messages, support tickets and notification tokens. Your images are also deleted from storage. All that remains: what was reported, as evidence (up to {{evidence_days}} days), reports made by others (up to {{report_retention}}), financial records (without the email), and a line in the audit log saying an account was deleted (public ID only).

# 10. Contact
From the app: **My account → Help and support**{{support_line}}."""

TERMS = """# 1. Acceptance
By using {{app}}, you accept these terms, the Privacy Policy and the Community Guidelines.

# 2. Eligibility
You must be 18 or older. One account per person, and you're responsible for keeping your password safe.

# 3. Your content
You're responsible for what you post and send. You allow {{app}} to display the content you publish within the platform. We may hide or delete any content that breaks the guidelines, and suspend or ban accounts that break them.

# 4. Not investment advice
Market prices, posts, analysis and comments are **for information only and are not investment advice**, nor a recommendation to buy or sell. Your trading decisions are yours alone, and {{app}} offers no trading or custody service.

# 5. Privacy between users
It's forbidden to reveal another user's identity, publish their data or try to find it out, or to record and share conversations.

# 6. Membership
The membership is a one-time payment ({{membership_price}} USDT at the moment) that gives **in-app features only** (currently: {{member_features}}). **It has nothing to do with trading or with any profit or financial return**, and it is not part of the criteria for any reward. {{refund_policy}} Details in the [Membership Terms](/policies/membership_terms).

# 7. Promotional rewards and withdrawals
Promotional rewards, **completely separate from the membership**, unrelated to trading and not guaranteed. Details in the [Rewards Terms](/policies/rewards_terms) and the [Withdrawal Terms](/policies/withdraw_terms).

# 8. The red envelope
A free contest that requires no membership and no payment. Details in the [Red Envelope Terms](/policies/giveaway_terms).

# 9. The service
We provide the service "as is" and work to keep it running, but it may sometimes stop for maintenance. These terms may change, and we'll ask for your agreement again in the app after any significant change.

# 10. Contact
My account → Help and support{{support_line}}."""

GUIDELINES = """# Allowed and welcome
Discussion, analysis, market news and ideas, shared respectfully, along with experiences and opinions.

# Not allowed
- Threats, blackmail, harassment, intimidation and bullying.
- Scams and promises of guaranteed profit, fundraising, and asking for passwords or wallet seed phrases.
- Sexual, pornographic or nude content, in ideas or images.
- **Any sexual or abusive content involving a minor: it's deleted immediately, its author is permanently banned, and a copy is kept as evidence.**
- Hate speech and incitement to violence.
- Posting or asking for phone numbers, accounts, addresses or personal data, yours or anyone else's.
- Promotional links, ads, spam and fake accounts.
- Impersonating {{app}} or its team, or using star-like symbols in names.

# Images
Posting an image with an idea needs a permission the admin team grants account by account (not linked to the membership), up to {{idea_image_limit}} every 24 hours, and it's reviewed by the moderation team. Sending images in chats is turned off. Anything that looks like it breaks the rules is rejected automatically.

# Reports
Use the "Report" button on any content. Reports are confidential. After {{report_threshold}} reports from different people, the content is hidden temporarily until it's reviewed.

# Penalties
Depending on how serious it is: content removal, a temporary suspension, a permanent ban, or losing the star."""

VERIFICATION = """# What is the blue star?
A badge meaning the account is **a member or a trusted account within {{app}}**. **It is not a real-identity check**: we never ask for any document, real name or ID photo.

# How do I get it?
The star comes with the **membership** (My account → My membership), as long as this feature is on. Anyone who had the star before the membership keeps it. A membership star goes away if the membership ends or is refunded, and any star can be removed for breaking the rules.

# Where does it show?
On the profile, posts, comments, the users page and conversations."""

MEMBERSHIP_TERMS = """# What is the membership?
A one-time payment ({{membership_price}} USDT at the moment) for **in-app features only**, currently: {{member_features}}. Features may be added or turned off for all members at once; it shows here.

Posting images with ideas isn't a membership feature: it's a permission the admin team grants account by account.

# What it is not
The membership **is not linked to any profit or return**, nor to trading. It gives no right to any reward and is not part of the criteria for any reward or contest.

# Payment
Pay only in the currency and on the network shown in "My membership", then enter the transaction ID (TXID). Funds sent on another network or to another address may be lost and can't be recovered. The team checks the payment, then activates the membership.

# Refund
{{refund_policy}} Request it from "My membership" with your password and a code sent to your email; the team then sends the amount to the address you enter."""

REWARDS_TERMS = """# Promotional rewards
Rewards offered by {{app}} for promotion, **completely separate from the membership**. They are not a return on any payment or investment, and they're not guaranteed. The platform sets them under these terms and may change or stop them.

# Inviting a friend
- {{referral_reward}} USDT when a friend you invited becomes a member; it stays pending for {{referral_hold_days}} days before it becomes available.
- One level only, and no reward for inviting yourself.
- The reward is cancelled if the friend's membership is refunded.
- Suspicious invitations (same network, or unusual numbers) are reviewed by hand; fake or duplicate ones are cancelled and the account may be suspended.

# Contests and activity rewards
The team announces them in the app, and their criteria never depend on membership or payment."""

WITHDRAW_TERMS = """# Withdrawals
- Supported networks: {{withdraw_networks}} (USDT).
- Minimum: {{withdraw_min}} USDT, from the available balance only (pending amounts can't be withdrawn).
- Network fee: {{withdraw_fee}} USDT, deducted from the amount sent; the net amount is shown before you confirm.
- Confirmed with your password and a code sent to your email.

# Your address, your responsibility
Check the address and network before confirming: **a transfer to the wrong address or network is your responsibility** and can't be recovered. The {{app}} team will never ask you for your password or any code."""

GIVEAWAY_TERMS = """# The red envelope
- A **free** contest that requires no membership and no payment.
- One entry per person per round, with an email confirmed by a code.
- When the round ends, winners are drawn **at random** from the confirmed entries; suspended accounts and duplicate entries from the same network are excluded. The draw is logged for auditing.
- The prize code is sent by email only, and we will never ask you for it. Winners' names (display name only) may be shown."""

ABOUT = """# About {{app}}
A community for traders to share crypto news, analysis and ideas.
- **Market**: the top gainers and losers over 24 hours, for information only.
- **Ideas**: post your analysis or a piece of news, and discuss it in the comments.
- **Users and messages**: meet other traders and message them.

Your data is protected: nobody sees your email or any private data about you, except what you choose to show."""

FAQ = """# Does the membership earn money?
No. The membership only gives in-app features (currently: {{member_features}}), and it's not linked to any return.

# Does the app give trading advice?
No. The market and posts are for information and discussion only, and your decisions are your own.

# Who sees my email?
No other user. Only the admin team sees it, when needed to run and protect the service.

# How do I contact the team?
My account → Help and support, or My account → Contact us."""

CONTACT = """The fastest way: a **support ticket** from inside the app (My account → Help and support). The team replies there and by email.

Replies to {{app}}'s automatic emails (codes and notifications) are not read. We will never ask you for your password or any code."""

MEMBERSHIP_INTRO = ("The membership gives **features inside {{app}} only** (currently: {{member_features}}). "
                    "It's not linked to any profit or return, nor to trading.")
WELCOME = "A community for traders, crypto news and analysis. Nobody sees your email or your data."
MARKET_DISCLAIMER = "For information only, not investment advice. Prices come from Bybit and may be slightly delayed."

CONTENT: dict[str, tuple[str | None, str]] = {
    "privacy": ("Privacy Policy", PRIVACY),
    "terms": ("Terms of Use", TERMS),
    "guidelines": ("Community Guidelines", GUIDELINES),
    "membership_terms": ("Membership Terms", MEMBERSHIP_TERMS),
    "rewards_terms": ("Rewards Terms", REWARDS_TERMS),
    "withdraw_terms": ("Withdrawal Terms", WITHDRAW_TERMS),
    "giveaway_terms": ("Red Envelope Terms", GIVEAWAY_TERMS),
    "verification": ("The blue star", VERIFICATION),
    "about": ("About the app", ABOUT),
    "faq": ("FAQ", FAQ),
    "contact": ("Contact us", CONTACT),
    "market_disclaimer": (None, MARKET_DISCLAIMER),
    "membership_intro": (None, MEMBERSHIP_INTRO),
    "welcome": (None, WELCOME),
    "announcement": (None, ""),
    "profile_footer": (None, ""),
    # e-mails: (subject, body) — the same {variables} as the Arabic texts
    "email.code": (
        "{app}: confirmation code {code}",
        "Hello,\n\nConfirmation code ({purpose}): {code}\n\nValid for {minutes} minutes. Don't share it with anyone: the {app} "
        "team will never ask you for it.\nIf you didn't request it, ignore this message and change your password.\n\n"
        "The {app} team",
    ),
    "email.signup_code": (
        "{app}: your account confirmation code {code}",
        "Hello,\n\nYour code to confirm your new {app} account:\n\n{code}\n\nEnter it in the app to finish signing up. It is valid for {minutes} minutes. Don’t share it with anyone: the {app} team will never ask you for it.\nIf you didn’t ask to create an account, ignore this message: no account will be created.\n\nThe {app} team",
    ),
    "email.reset": (
        "Your {app} account recovery code",
        "Your {app} account recovery code:\n\n    {code}\n\nEnter this code in the app to choose a new password. It's valid for "
        "{hours} hour(s) and can be used once.\nIf you didn't ask to recover your account, ignore this message; your current "
        "password hasn't changed.\n\nThe {app} team",
    ),
    "email.membership_accepted": (
        "{app}: membership",
        "Hello,\n\nYour membership is now active. Member features are available to you in the app.\n\nThe {app} team",
    ),
    "email.membership_rejected": (
        "{app}: membership",
        "Hello,\n\nWe couldn't accept your membership request. Reason: {note}\n\nThe {app} team",
    ),
    "email.membership_topup": (
        "{app}: complete your membership payment",
        "Hello,\n\nWe received your membership payment, but it is below the price. Missing amount: {amount} USDT.\n\nSend it on the {network} network only, to the same address:\n{wallet}\n\nThen open {app} → My account → My membership and add the transaction ID (TXID) to the same request. Don't send it on another network: the funds would be lost.\n\nThe {app} team",
    ),
    "email.notice": (
        "{app}: {topic}",
        "Hello,\n\n{message}\n\nThe {app} team",
    ),
    "email.giveaway": (
        "{app}: your red envelope prize",
        "Congratulations! 🧧\n\nYou won in \"{title}\". Your prize code:\n\n{code}\n\nDon't share it with anyone. The {app} team "
        "will never ask you for it.\n\nThe {app} team",
    ),
    "email.ticket_new": (
        "[{app} #{ticket}] {subject}",
        "{kind} #{ticket}\nType: {category}\nPublic ID: {public_id}\n\n{message}\n\n"
        "— Replying to this email goes straight to the user's email. To reply inside the app, use the dashboard → Support.",
    ),
    "email.ticket_reply": (
        "[{app} #{ticket}] Your ticket has a reply",
        "Hello,\n\nThe support team replied to your ticket #{ticket} ({subject}).\nOpen {app} → My account → Support → My tickets "
        "to read the reply.\n\nThe {app} team",
    ),
    "email.test": (
        "{app}: test email ({mailbox})",
        "The {mailbox} settings in {app} work.",
    ),
    "email.footer": (
        None,
        "This is an automatic message, please don't reply. To contact us, use \"Support\" in the app{support_line}.",
    ),
}
