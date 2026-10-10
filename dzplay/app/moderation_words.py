"""Built-in word lists for automatic flagging (app/services/moderation.py).

A hit never blocks a message: it only queues a copy for the owner's review in
the admin dashboard. Lists cover Arabic, Algerian Darja (Arabic letters and
Arabizi), French and English. Terms are normalised the same way as messages
(see moderation.normalize), so spelling variants such as أ/ا or ة/ه and
repeated letters do not matter.

* A single word matches a whole word (common prefixes like و / ال / يا and
  suffixes like ك / ها are tried too).
* A term ending in "*" matches any word starting with it.
* A term with spaces matches that exact phrase.

Owners can add their own words with MODERATION_EXTRA_WORDS in .env.
"""

from __future__ import annotations

THREAT = [
    # Arabic / Darja
    "نقتلك", "نقتلكم", "اقتلك", "ساقتلك", "سوف اقتلك", "راح نقتلك", "غادي نقتلك", "نذبحك", "ندبحك", "اذبحك",
    "نحرقك", "نحرق دارك", "نطيحلك", "نكسرلك راسك", "نكسر راسك", "نضربك", "نضربك بالموس", "نجيك للدار",
    "نعرف وين تسكن", "نعرف وين ساكن", "نعرف وين تقرا", "نعرف دارك", "راك ميت", "راكي ميته", "تموت اليوم",
    "نخليك تندم", "نتبعك", "نتربصلك", "قنبله", "نفجر",
    # Arabizi
    "n9tlek", "n9atlek", "nkatlek", "nqtlek", "ndab7ek", "ndeb7ek", "nedb7ek", "nahrgek", "n7rgek", "nadarbek",
    "rak mayet", "rak mit",
    # French / English
    "je vais te tuer", "je te tue", "te tuer", "t'es mort", "tu es mort", "je sais ou tu habites",
    "kill you", "i will kill", "gonna kill", "i know where you live", "you are dead", "you're dead",
]

BLACKMAIL = [
    "نفضحك", "نفضحكم", "فضيحه", "فضيحتك", "ننشر صورك", "نشر صورك", "صورك", "تصاويرك", "تصاورك", "ننشر تصاويرك",
    "نبعث صورك", "نقول لاهلك", "نقول لباباك", "نقول لمك", "نوري لباباك", "نبعثهم لباباك", "ابتزاز", "ولا ننشر",
    "ولا نفضحك", "خلصني ولا", "ابعث الدراهم", "ابعثلي الدراهم", "فليكسي ولا",
    "nfd7ek", "nfad7ek", "nfde7ek", "nfdhek", "tsawrek", "tsawrk", "tswarek", "nnachar tsawrek",
    "je vais publier", "tes photos", "sinon je publie", "your photos", "send money or", "pay me or",
]

SEXUAL = [
    "زب", "زبي", "زبك", "كس", "كسك", "كسمك", "طيز", "طيزك", "نيك", "نيكك", "ننيكك", "نيكمك", "قحب*", "شرموط*",
    "عاهره", "سكس", "بزازل", "نحويك", "نحوي", "حواك", "حوايه", "تتحوى", "صورتك عريانه", "صوره عاريه",
    "ابعثلي صورتك", "ابعثيلي صورتك", "ابعثلي صوره", "ابعثيلي صوره", "وريني جسمك", "نبغي نرقد معاك",
    "nik", "nikmok", "nikomok", "nikek", "zeb", "zebi", "zbi", "9ahba", "qahba", "kahba", "n7wik", "n7wi",
    "7wa", "t7wa", "bzazel", "sex", "nude", "nudes", "pussy", "dick", "suck", "je vais te baiser", "suce",
    "send nudes", "send pic",
]

INSULT = [
    "يا كلب", "ولد الكلب", "ولد الكلبه", "بنت الكلب", "يا حمار", "يا حيوان", "حقير", "يا وسخ", "زامل", "ميبون",
    "مبون", "يلعن بوك", "يلعن دينك", "يلعن دين", "نعل بوك", "نعلبوك", "نعل دينك", "دين امك", "دين يماك", "تفو",
    "شماته", "يا بغل", "خنزير",
    "zamel", "miboun", "mibon", "nal bouk", "nalbouk", "nal dinek", "yel3an", "yl3an", "chmata", "tfou",
    "connard", "connasse", "pute", "salope", "fdp", "fils de pute", "nique", "ntm", "nique ta mere",
    "bitch", "fuck", "fucking", "asshole", "bastard", "motherfucker",
]

# Moving a stranger off the app (or revealing who someone is) — words only;
# phone numbers, e-mails, @handles and links are detected with patterns.
CONTACT = [
    "سناب", "سنابشات", "انستا", "انستغرام", "انستقرام", "تيليجرام", "تلغرام", "تليغرام", "واتساب", "واتس",
    "فايبر", "ماسنجر", "فيسبوك", "تيك توك", "رقمك", "رقمي", "اعطيني رقمك", "عطيني نمرتك", "نمرتك", "نمرتي",
    "snap", "snapchat", "insta", "instagram", "telegram", "whatsapp", "whats", "viber", "messenger", "facebook",
    "tiktok", "numero", "num dyalek", "ton numero", "your number",
]

CATEGORIES: dict[str, list[str]] = {
    "threat": THREAT,
    "blackmail": BLACKMAIL,
    "sexual": SEXUAL,
    "insult": INSULT,
    "contact": CONTACT,
}
