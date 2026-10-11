"""V6 phase 12: French versions of the content system's default texts (see app/i18n/content.py)."""

PRINCIPLE = ("{{app}} est une communauté de traders pour partager l’actualité des cryptomonnaies, des analyses et des idées, "
             "en protégeant vos données : aucun utilisateur ne voit votre e-mail ni aucune donnée privée vous concernant, "
             "à part ce que vous choisissez vous-même d’afficher (nom, photo, publications).")

PRIVACY = """# 1. Ce que nous collectons et pourquoi
- **L’e-mail et un mot de passe chiffré** (ou l’identifiant Google) : uniquement pour la connexion, la récupération du compte, les réponses du support et les notifications du compte. Ils ne sont visibles par aucun utilisateur.
- **Le nom affiché et l’identifiant public (DZ-…)**, la photo de profil si vous en ajoutez une, et le genre si vous le choisissez : visibles par les autres parce que vous l’avez choisi.
- **La confirmation de l’âge (18+)** : nous ne demandons pas votre date de naissance.
- **Ce que vous publiez** : les idées et leurs images, les commentaires et réponses, les « J’aime » (on voit qui a aimé une publication ; les « Je n’aime pas » ne sont qu’un nombre, sans noms).
- **Les messages** : temporaires ; ils sont supprimés du serveur au plus tard après {{message_ttl}}, et plus vite une fois lus ({{message_ttl_read}}). Une conversation inactive est supprimée après {{conversation_idle}}. Les messages anonymes aléatoires ont été arrêtés ; les anciennes conversations anonymes restent en lecture seule puis sont supprimées après {{legacy_anon_days}} jours.
- **Les adresses IP** : nous ne les conservons pas telles quelles ; nous gardons seulement une empreinte chiffrée et irréversible, pour prévenir les abus et les tentatives automatisées.
- **Les notifications du téléphone** (facultatives) : seulement le jeton de l’appareil nécessaire pour les acheminer ; une notification ne contient ni nom ni texte de message.
- **Le support** : le texte du ticket, transmis à l’e-mail de l’équipe du support avec votre seul identifiant public.
- **L’adhésion et les retraits** : le montant, le réseau et l’identifiant de transaction (TXID), ainsi que l’adresse de portefeuille que vous saisissez pour un remboursement ou un retrait. Nous ne demandons ni vrai nom ni document. Ces enregistrements financiers sont conservés à des fins comptables, même après la suppression du compte (sans e-mail).

# 2. Ce que voient les autres
Votre nom affiché, votre photo, votre identifiant public, l’icône de genre si vous l’avez choisie et l’étoile bleue si vous l’avez : dans les publications, les commentaires, la page des utilisateurs et les conversations. Personne ne voit votre e-mail, votre numéro, votre position ni aucun identifiant interne.

# 3. Accès de l’équipe d’administration
Pour faire fonctionner le service, le modérer et protéger les utilisateurs, l’équipe d’administration peut consulter les données des comptes (dont l’e-mail), les publications et commentaires, les messages tant qu’ils sont conservés, les images publiées, et uniquement les images de conversation qui ont été signalées. L’accès est protégé par un compte administrateur et une validation en deux étapes, et chaque consultation et chaque action sont consignées dans un journal d’audit interne. Les messages, commentaires et images sont analysés automatiquement pour détecter les contenus nuisibles.

# 4. Prestataire de stockage externe
Les images sont stockées dans un **canal privé Telegram** appartenant à {{app}}, auquel seul le bot de la plateforme a accès. Notre serveur fait transiter les fichiers et n’en garde qu’une copie temporaire pour accélérer l’affichage. Votre application ne se connecte jamais à Telegram, et aucun utilisateur n’obtient la référence du fichier. Une copie des images publiées et des photos de profil (pas des images de conversation) est envoyée à un groupe privé de modérateurs pour examen.

# 5. Suppression des métadonnées et vérification
Avant l’enregistrement, chaque image est réencodée et toutes ses métadonnées sont supprimées : position GPS, modèle d’appareil, date, etc. Le fichier est vérifié sur votre appareil puis sur le serveur par un modèle automatique de détection de contenu pornographique, et ce qui enfreint les règles est refusé avant d’être enregistré. Aucune vérification automatique ne permet de déterminer l’âge des personnes ; nous nous appuyons donc sur les signalements et l’examen humain.

# 6. Images de conversation
- L’envoi d’images dans les conversations est réservé aux membres, et tout utilisateur peut les recevoir et les ouvrir.
- Elles arrivent floutées, ne s’affichent que lorsque le destinataire appuie dessus, puis restent visibles {{chat_ttl}} secondes seulement avant de disparaître des deux côtés.
- Une image jamais ouverte est supprimée après {{chat_unopened}}.
- Une fois expirée, elle est immédiatement supprimée du serveur et des appareils, et la copie de stockage est effacée après un court délai de signalement ({{chat_grace_min}} minute(s)) pendant lequel personne ne la voit.
- {{chat_archive}}
- Une image signalée est conservée comme preuve pour l’examen jusqu’à {{evidence_days}} jours.
- Dans l’application Android, les captures d’écran sont bloquées pendant l’affichage, mais il est impossible d’empêcher de photographier l’écran avec un autre appareil : n’envoyez rien que vous n’accepteriez pas de voir circuler.

# 7. Âge
{{app}} est réservé aux adultes (18 ans et plus).

# 8. Durées de conservation
- Messages et conversations : comme indiqué au point 1.
- Signalements : {{report_retention}}. Événements de sécurité : {{security_retention}}. Journal d’audit de l’administration : {{audit_retention}}.
- Copies temporaires sur le serveur : supprimées après {{cache_ttl}} sans utilisation.
- Le compte et ce que vous publiez : tant que votre compte existe.

# 9. Suppression du compte
Depuis **Mon compte → Supprimer mon compte**. Sont supprimés immédiatement : le compte et les sessions, le profil, le nom et la photo, les idées avec leurs images, commentaires et réactions, vos commentaires et réactions, les conversations et messages, les tickets de support et les jetons de notification. Vos images sont aussi supprimées du stockage. Ne restent que : ce qui a été signalé, comme preuve (jusqu’à {{evidence_days}} jours), les signalements faits par d’autres (jusqu’à {{report_retention}}), les enregistrements financiers (sans e-mail), et une ligne dans le journal d’audit indiquant qu’un compte a été supprimé (identifiant public uniquement).

# 10. Contact
Depuis l’application : **Mon compte → Aide et support**{{support_line}}."""

TERMS = """# 1. Acceptation
En utilisant {{app}}, vous acceptez ces conditions, la politique de confidentialité et les règles de la communauté.

# 2. Conditions d’accès
Vous devez avoir 18 ans ou plus. Un seul compte par personne, et vous êtes responsable de la protection de votre mot de passe.

# 3. Votre contenu
Vous êtes responsable de ce que vous publiez et envoyez. Vous autorisez {{app}} à afficher votre contenu publié au sein de la plateforme. Nous pouvons masquer ou supprimer tout contenu contraire aux règles, et suspendre ou bannir les comptes en infraction.

# 4. Pas un conseil en investissement
Les prix du marché, les publications, les analyses et les commentaires sont **fournis à titre d’information uniquement et ne constituent pas un conseil en investissement**, ni une recommandation d’achat ou de vente. Vos décisions de trading relèvent de votre seule responsabilité, et {{app}} ne propose aucun service de trading ni de garde de fonds.

# 5. Vie privée entre utilisateurs
Il est interdit de révéler l’identité d’un autre utilisateur, de publier ses données ou de chercher à les connaître, ainsi que d’enregistrer et de diffuser les conversations et les images éphémères.

# 6. Adhésion
L’adhésion est un paiement unique ({{membership_price}} USDT actuellement) qui donne accès à des **fonctionnalités dans l’application uniquement** : l’étoile bleue, la publication d’idées avec une image et l’envoi d’images dans les conversations. **Elle n’a aucun lien avec le trading ni avec un quelconque gain ou rendement financier**, et n’entre dans les critères d’aucune récompense. {{refund_policy}} Détails dans les [conditions de l’adhésion](/policies/membership_terms).

# 7. Récompenses promotionnelles et retraits
Des récompenses promotionnelles **totalement indépendantes de l’adhésion**, sans lien avec le trading et non garanties. Détails dans les [conditions des récompenses](/policies/rewards_terms) et les [conditions de retrait](/policies/withdraw_terms).

# 8. L’enveloppe rouge
Un concours gratuit, sans adhésion ni paiement. Détails dans les [conditions de l’enveloppe rouge](/policies/giveaway_terms).

# 9. Le service
Nous fournissons le service « en l’état » et faisons en sorte qu’il reste disponible, mais il peut être interrompu ponctuellement pour maintenance. Ces conditions peuvent évoluer ; en cas de changement important, nous vous demandons à nouveau votre accord dans l’application.

# 10. Contact
Mon compte → Aide et support{{support_line}}."""

GUIDELINES = """# Autorisé et bienvenu
La discussion, l’analyse, l’actualité du marché et les idées, dans le respect, ainsi que le partage d’expériences et d’opinions.

# Interdit
- Les menaces, le chantage, le harcèlement, les intimidations et le dénigrement.
- Les arnaques et les promesses de gains garantis, les collectes d’argent, et le fait de demander des mots de passe ou des phrases secrètes de portefeuille.
- Les contenus sexuels, pornographiques ou de nudité, dans les idées, les images ou les images de conversation.
- **Tout contenu sexuel ou abusif impliquant un mineur : il est supprimé immédiatement, son auteur est banni définitivement et une copie est conservée comme preuve.**
- Les discours de haine et l’incitation à la violence.
- La publication ou la demande de numéros de téléphone, de comptes, d’adresses ou de données personnelles, les vôtres comme celles des autres.
- Les liens promotionnels, les publicités, les messages indésirables et les faux comptes.
- Se faire passer pour {{app}} ou son équipe, ou utiliser dans les noms des symboles ressemblant à l’étoile.

# Images
Une image avec une idée toutes les 24 heures (actuellement {{idea_image_limit}}) pour les membres, examinée par l’équipe de modération. Les images de conversation sont éphémères et disponibles une fois que l’autre personne a répondu. Ce qui semble contraire aux règles est refusé automatiquement.

# Signalements
Utilisez le bouton « Signaler » sur n’importe quel contenu. Les signalements sont confidentiels. À partir de {{report_threshold}} signalements de personnes différentes, le contenu est masqué temporairement jusqu’à son examen.

# Sanctions
Selon la gravité : suppression du contenu, suspension temporaire, bannissement définitif ou retrait de l’étoile."""

VERIFICATION = """# Qu’est-ce que l’étoile bleue ?
Un badge qui indique que le compte est **membre ou compte de confiance au sein de {{app}}**. **Ce n’est pas une vérification de l’identité réelle** : nous ne demandons aucun document, aucun vrai nom et aucune photo d’identité.

# Comment l’obtenir ?
L’étoile est incluse dans **l’adhésion** (Mon compte → Mon adhésion). Ceux qui avaient l’étoile avant l’adhésion la conservent. L’étoile liée à l’adhésion disparaît si l’adhésion prend fin ou est remboursée, et toute étoile peut être retirée en cas d’infraction.

# Où apparaît-elle ?
Sur le profil, les publications, les commentaires, la page des utilisateurs et les conversations."""

MEMBERSHIP_TERMS = """# Qu’est-ce que l’adhésion ?
Un abonnement à paiement unique ({{membership_price}} USDT actuellement) qui donne accès à des **fonctionnalités dans l’application uniquement** :
- L’étoile bleue à côté de votre nom.
- La publication d’idées avec une image.
- L’envoi d’images dans les conversations (elles disparaissent {{chat_ttl}} secondes après leur ouverture).

# Ce qu’elle n’est pas
L’adhésion **n’est liée à aucun gain ni rendement**, ni au trading. Elle ne donne droit à aucune récompense et n’entre dans les critères d’aucune récompense ni d’aucun concours.

# Paiement
Le paiement se fait uniquement dans la devise et sur le réseau indiqués dans « Mon adhésion », puis vous saisissez l’identifiant de transaction (TXID). Un envoi sur un autre réseau ou vers une autre adresse peut être perdu sans possibilité de récupération. L’équipe vérifie le paiement puis active l’adhésion.

# Remboursement
{{refund_policy}} La demande se fait depuis « Mon adhésion » avec votre mot de passe et un code envoyé à votre e-mail ; l’équipe envoie ensuite le montant à l’adresse que vous indiquez."""

REWARDS_TERMS = """# Récompenses promotionnelles
Des récompenses offertes par {{app}} à des fins promotionnelles, **totalement indépendantes de l’adhésion**. Elles ne sont le rendement d’aucun paiement ni d’aucun investissement, et ne sont pas garanties. La plateforme les fixe selon ces conditions et peut les modifier ou les arrêter.

# Parrainer un ami
- {{referral_reward}} USDT lorsqu’un ami que vous avez invité devient membre ; la récompense reste en attente {{referral_hold_days}} jours avant d’être disponible.
- Un seul niveau, et aucune récompense pour vous inviter vous-même.
- La récompense est annulée si l’adhésion de l’ami est remboursée.
- Les invitations suspectes (même réseau ou volumes anormaux) sont examinées manuellement ; les fausses invitations ou les doublons sont annulés et le compte peut être suspendu.

# Concours et récompenses d’activité
L’équipe les annonce dans l’application, et leurs critères ne dépendent jamais de l’adhésion ni d’un paiement."""

WITHDRAW_TERMS = """# Retraits
- Réseaux pris en charge : {{withdraw_networks}} (USDT).
- Minimum : {{withdraw_min}} USDT, sur le solde disponible uniquement (le montant en attente ne peut pas être retiré).
- Frais de réseau : {{withdraw_fee}} USDT, déduits du montant envoyé ; le montant net est affiché avant la confirmation.
- Confirmation par mot de passe et par un code envoyé à votre e-mail.

# Responsabilité de l’adresse
Vérifiez l’adresse et le réseau avant de confirmer : **un envoi vers une mauvaise adresse ou un mauvais réseau relève de votre responsabilité** et ne peut pas être récupéré. L’équipe {{app}} ne vous demandera jamais votre mot de passe ni aucun code."""

GIVEAWAY_TERMS = """# L’enveloppe rouge
- Un concours **gratuit**, sans adhésion ni paiement.
- Une participation par personne et par tour, avec un e-mail confirmé par un code.
- À la fin du tour, les gagnants sont tirés **au hasard** parmi les participations confirmées ; les comptes suspendus et les participations en double depuis le même réseau sont exclus. Le tirage est enregistré pour contrôle.
- Le code du prix est envoyé uniquement par e-mail, et nous ne vous le demanderons jamais. Les noms des gagnants (nom affiché uniquement) peuvent être publiés."""

ABOUT = """# À propos de {{app}}
Une communauté de traders pour partager l’actualité des cryptomonnaies, des analyses et des idées.
- **Marché** : les plus fortes hausses et baisses sur 24 heures, à titre d’information uniquement.
- **Idées** : publiez votre analyse ou une actualité, et discutez-en dans les commentaires.
- **Utilisateurs et messages** : découvrez d’autres traders et écrivez-leur.

Vos données sont protégées : personne ne voit votre e-mail ni aucune donnée privée vous concernant, à part ce que vous choisissez d’afficher."""

FAQ = """# L’adhésion rapporte-t-elle des gains ?
Non. L’adhésion donne uniquement des fonctionnalités dans l’application (l’étoile, les images dans les idées et les conversations), sans aucun lien avec un rendement.

# L’application donne-t-elle des conseils de trading ?
Non. Le marché et les publications servent uniquement à s’informer et à discuter ; vos décisions relèvent de votre responsabilité.

# Qui voit mon e-mail ?
Aucun utilisateur. Seule l’équipe d’administration le voit, lorsque c’est nécessaire pour faire fonctionner et protéger le service.

# Comment contacter l’équipe ?
Mon compte → Aide et support, ou Mon compte → Nous contacter."""

CONTACT = """Le plus rapide : un **ticket de support** depuis l’application (Mon compte → Aide et support). L’équipe vous répond ici et par e-mail.

Les réponses aux messages automatiques de {{app}} (codes et notifications) ne sont pas lues. Nous ne vous demanderons jamais votre mot de passe ni aucun code."""

MEMBERSHIP_INTRO = ("L’adhésion donne accès à des **fonctionnalités dans {{app}} uniquement** : l’étoile bleue, la publication d’idées "
                    "avec une image et l’envoi d’images dans les conversations. Elle n’est liée à aucun gain ni rendement, ni au trading.")
WELCOME = "Une communauté de traders, l’actualité des cryptomonnaies et des analyses. Personne ne voit votre e-mail ni vos données."
MARKET_DISCLAIMER = "À titre d’information uniquement, ce n’est pas un conseil en investissement. Les prix proviennent de Bybit et peuvent avoir un léger retard."

CONTENT: dict[str, tuple[str | None, str]] = {
    "privacy": ("Politique de confidentialité", PRIVACY),
    "terms": ("Conditions d’utilisation", TERMS),
    "guidelines": ("Règles de la communauté", GUIDELINES),
    "membership_terms": ("Conditions de l’adhésion", MEMBERSHIP_TERMS),
    "rewards_terms": ("Conditions des récompenses", REWARDS_TERMS),
    "withdraw_terms": ("Conditions de retrait", WITHDRAW_TERMS),
    "giveaway_terms": ("Conditions de l’enveloppe rouge", GIVEAWAY_TERMS),
    "verification": ("L’étoile bleue", VERIFICATION),
    "about": ("À propos de l’application", ABOUT),
    "faq": ("Questions fréquentes", FAQ),
    "contact": ("Nous contacter", CONTACT),
    "market_disclaimer": (None, MARKET_DISCLAIMER),
    "membership_intro": (None, MEMBERSHIP_INTRO),
    "welcome": (None, WELCOME),
    "announcement": (None, ""),
    "profile_footer": (None, ""),
    # e-mails: (subject, body) — the same {variables} as the Arabic texts
    "email.code": (
        "{app} : code de confirmation {code}",
        "Bonjour,\n\nCode de confirmation ({purpose}) : {code}\n\nValable {minutes} minutes. Ne le partagez avec personne : "
        "l’équipe {app} ne vous le demandera jamais.\nSi vous n’êtes pas à l’origine de cette demande, ignorez ce message et "
        "changez votre mot de passe.\n\nL’équipe {app}",
    ),
    "email.signup_code": (
        "{app} : code de confirmation de votre compte {code}",
        "Bonjour,\n\nCode de confirmation de la création de votre compte {app} :\n\n{code}\n\nSaisissez-le dans l’application pour terminer l’inscription. Il est valable {minutes} minutes. Ne le partagez avec personne : l’équipe {app} ne vous le demandera jamais.\nSi vous n’avez pas demandé à créer un compte, ignorez ce message : aucun compte ne sera créé.\n\nL’équipe {app}",
    ),
    "email.reset": (
        "Code de récupération de votre compte {app}",
        "Code de récupération de votre compte {app} :\n\n    {code}\n\nSaisissez ce code dans l’application pour choisir un "
        "nouveau mot de passe. Il est valable {hours} heure(s) et à usage unique.\nSi vous n’avez pas demandé à récupérer votre "
        "compte, ignorez ce message ; votre mot de passe actuel n’a pas changé.\n\nL’équipe {app}",
    ),
    "email.membership_accepted": (
        "{app} : adhésion",
        "Bonjour,\n\nVotre adhésion est activée. Les fonctionnalités réservées aux membres sont désormais disponibles (l’étoile "
        "bleue, les idées avec image, les images dans les conversations).\n\nL’équipe {app}",
    ),
    "email.membership_rejected": (
        "{app} : adhésion",
        "Bonjour,\n\nNous n’avons pas pu accepter votre demande d’adhésion. Motif : {note}\n\nL’équipe {app}",
    ),
    "email.membership_topup": (
        "{app} : complétez le paiement de l’adhésion",
        "Bonjour,\n\nNous avons bien reçu le paiement de votre adhésion, mais il est inférieur au prix. Montant manquant : {amount} USDT.\n\nEnvoyez-le uniquement sur le réseau {network}, à la même adresse :\n{wallet}\n\nOuvrez ensuite {app} → Mon compte → Mon adhésion et ajoutez l’identifiant de transaction (TXID) à la même demande. N’envoyez pas sur un autre réseau : le montant serait perdu.\n\nL’équipe {app}",
    ),
    "email.notice": (
        "{app} : {topic}",
        "Bonjour,\n\n{message}\n\nL’équipe {app}",
    ),
    "email.giveaway": (
        "{app} : votre prix de l’enveloppe rouge",
        "Félicitations ! 🧧\n\nVous avez gagné dans « {title} ». Votre code de prix :\n\n{code}\n\nNe le partagez avec personne. "
        "L’équipe {app} ne vous le demandera jamais.\n\nL’équipe {app}",
    ),
    "email.ticket_new": (
        "[{app} #{ticket}] {subject}",
        "{kind} #{ticket}\nType : {category}\nIdentifiant public : {public_id}\n\n{message}\n\n"
        "— Répondre à cet e-mail écrit directement à l’utilisateur. Pour répondre dans l’application, utilisez le tableau de bord → Support.",
    ),
    "email.ticket_reply": (
        "[{app} #{ticket}] Réponse à votre ticket",
        "Bonjour,\n\nL’équipe du support a répondu à votre ticket #{ticket} ({subject}).\nOuvrez {app} → Mon compte → Support → "
        "Mes tickets pour lire la réponse.\n\nL’équipe {app}",
    ),
    "email.test": (
        "{app} : e-mail de test ({mailbox})",
        "Les réglages de {mailbox} dans {app} fonctionnent.",
    ),
    "email.footer": (
        None,
        "Ceci est un message automatique, merci de ne pas y répondre. Pour nous contacter, utilisez « Support » dans "
        "l’application{support_line}.",
    ),
}
