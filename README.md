# Miri Affiliations **ULTIME (FR)**
Bot d'affiliation 100% **français** (Discord + API) :
- Commandes et textes **entièrement en français**
- Panneau **Propriétaires** (ajout/suppression, backup BDD, rotation clé API, stats)
- Intégration **Rencontre** & **Epic coins**
- Arbres généalogiques UHQ (thèmes, avatars, RTL, export 1x/2x/3x)
- REST API sécurisée (X-Secret), Railway-ready, SQLite

## Démarrer
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
python bot.py
```

## Commandes (FR)
- `/proposer_relation @membre type:<mariage|ami|frere_soeur> wallet:<true|false>`
- `/famille_creer nom:"..." wallet:<true|false>` · `/famille_inviter relation_id:"..." @membre`
- `/lien_parente ajouter_parent enfant:@X parent:@Y` · `retirer_parent` · `lister user:@X`
- `/arbre_famille relation_id:"..." theme:<kawaii|sakura|royal|neon|arabesque> rtl:<true|false> avatars:<true|false> res:<1|2|3> public:<true|false>`
- `/proposer_divorce @partenaire split_mode:<egal|pourcentage> percent_pour_toi:<0-100> penalite_coins:<N> payeur_cest_moi:<true|false> expire_minutes:<5-1440>`
- `/reglages_aff definir_theme|definir_rtl|definir_avatars|definir_salon_logs`
- `/proprietaires ajouter|retirer|lister|sauvegarder_bdd|definir_clef_api|stats`
