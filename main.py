import os
import requests
import time
import functools
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

ODDS_KEY = os.getenv("ODDS_API_KEY")
MOON_KEY = os.getenv("MOONSHOT_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")

SPORTS = [
    'soccer_epl',
    'soccer_spain_la_liga',
    'soccer_germany_bundesliga',
    'soccer_italy_serie_a',
    'tennis_atp',
    'tennis_wta',
    'basketball_nba',
    'americanfootball_nfl',
    'handball_euro_championship',
    'badminton',
    'darts'
]

@functools.lru_cache(maxsize=32)
def _get_odds_cached(sport, ttl_hash):
    try:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
        params = {
            'regions': 'eu,us',
            'markets': 'h2h',
            'oddsFormat': 'decimal',
            'apiKey': ODDS_KEY
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception as e:
        print(f"Error fetching odds for {sport}: {e}")
        return []

def get_odds(sport):
    ttl_hash = int(time.time()) // 900
    return _get_odds_cached(sport, ttl_hash)

def naive_edge(game):
    try:
        if not game.get('bookmakers') or len(game['bookmakers']) == 0:
            return None

        outcome_names = set()
        for bookmaker in game['bookmakers']:
            for outcome in bookmaker['markets'][0]['outcomes']:
                outcome_names.add(outcome['name'])

        if len(outcome_names) < 2:
            return None

        outcome_names = list(outcome_names)

        best_odds_per_outcome = {}
        bookmaker_data = []

        for bookmaker in game['bookmakers']:
            outcomes_dict = {}
            for outcome in bookmaker['markets'][0]['outcomes']:
                outcomes_dict[outcome['name']] = outcome['price']

            implied_probs = {}
            for name in outcome_names:
                if name in outcomes_dict:
                    implied_probs[name] = 1 / outcomes_dict[name]
                else:
                    implied_probs[name] = 0

            total_implied = sum(implied_probs.values())
            if total_implied == 0:
                continue

            vig_free = {}
            for name in outcome_names:
                vig_free[name] = implied_probs[name] / total_implied

                if name in outcomes_dict:
                    price = outcomes_dict[name]
                    if name not in best_odds_per_outcome or price > best_odds_per_outcome[name]:
                        best_odds_per_outcome[name] = price

            bookmaker_data.append({
                'vig_free': vig_free,
                'outcomes_dict': outcomes_dict
            })

        if len(bookmaker_data) == 0:
            return None

        consensus_probs = {}
        for name in outcome_names:
            probs_for_consensus = []

            for bm_data in bookmaker_data:
                if name in bm_data['outcomes_dict']:
                    price = bm_data['outcomes_dict'][name]
                    if price == best_odds_per_outcome.get(name):
                        continue

                if bm_data['vig_free'][name] > 0:
                    probs_for_consensus.append(bm_data['vig_free'][name])

            if len(probs_for_consensus) == 0:
                vig_free_probs_all = [bm_data['vig_free'][name] for bm_data in bookmaker_data if bm_data['vig_free'][name] > 0]
                if len(vig_free_probs_all) == 0:
                    return None
                probs_for_consensus = vig_free_probs_all

            sorted_probs = sorted(probs_for_consensus)
            n = len(sorted_probs)

            if n == 1:
                consensus_probs[name] = sorted_probs[0]
            elif n % 2 == 0:
                median_prob = (sorted_probs[n // 2 - 1] + sorted_probs[n // 2]) / 2
                consensus_probs[name] = median_prob
            else:
                median_prob = sorted_probs[n // 2]
                consensus_probs[name] = median_prob

        total_consensus = sum(consensus_probs.values())
        if total_consensus == 0:
            return None

        fair_probs = {}
        for name in outcome_names:
            fair_probs[name] = consensus_probs[name] / total_consensus

        best_pick = None
        best_roi = -1
        best_odds = 0

        for name in outcome_names:
            if name not in best_odds_per_outcome:
                continue

            best_price = best_odds_per_outcome[name]
            fair_prob = fair_probs[name]

            expected_roi = (fair_prob * best_price) - 1

            if expected_roi > best_roi:
                best_roi = expected_roi
                best_pick = name
                best_odds = best_price

        if best_roi >= 0.05:
            return (best_pick, best_odds, best_roi)

        return None

    except Exception as e:
        print(f"Error calculating edge: {e}")
        return None

def kimi_tip(sport, pick, odds, edge):
    try:
        # Use a slightly adjusted prompt for better context with multiple picks
        prompt = (
            f"Write a short, engaging 2-sentence betting tip for {sport} in markdown format. "
            f"The pick is **{pick}** at odds {odds:.2f}, showing a value edge of {edge*100:.1f}%. "
            f"Include current form, H2H, or an injury note if widely known, and be persuasive."
        )
        headers = {
            "Authorization": f"Bearer {MOON_KEY}",
            "Content-Type": "application/json"
        }
        body = {
            "model": "moonshot-v1-8k",
            "messages": [{"role": "user", "content": prompt}]
        }
        r = requests.post(
            "https://api.moonshot.cn/v1/chat/completions",
            headers=headers,
            json=body,
            timeout=15
        )
        if r.status_code == 200:
            return r.json()['choices'][0]['message']['content'].strip()
        else:
            return f"Tip generation unavailable (Status: {r.status_code})."
    except Exception as e:
        print(f"Error generating tip: {e}")
        return "Analysis suggests high value in this pick based on odds comparison."

async def tips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This process will take longer due to multiple API calls, so use a detailed initial message.
    await update.message.reply_text("🔍 Analyzing odds across markets... This may take up to a minute to process all value picks and generate AI analysis. Please wait.")

    # Set a reasonable limit for API usage and message size
    MAX_PICKS_PER_SPORT = 5

    msg = []

    for sport in SPORTS:
        games = get_odds(sport)
        if not games:
            continue

        value_picks = []

        # 1. Collect all valid value picks for the sport
        for game in games:
            edge_result = naive_edge(game)
            if edge_result:
                pick, odds, edge = edge_result
                # Store (edge, game, pick, odds) to sort later
                value_picks.append((edge, game, pick, odds)) 

        if not value_picks:
            continue

        # 2. Sort by edge (ROI) in descending order and take the top N
        value_picks.sort(key=lambda x: x[0], reverse=True)
        top_picks = value_picks[:MAX_PICKS_PER_SPORT]

        # 3. Start building the message block for this sport
        sport_name = sport.upper().replace('_', ' ')
        sport_msg = [f"🏆 **{sport_name}** ({len(top_picks)} Value Picks Found)"]

        for i, (edge, game, pick, odds) in enumerate(top_picks):
            # Format time
            commence_time = game.get('commence_time', '')
            date_str = ''
            if commence_time:
                try:
                    # Convert to datetime object and format
                    match_time = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
                    date_str = match_time.strftime('%b %d, %H:%M UTC')
                except:
                    date_str = ''

            home_team = game.get('home_team', 'Team A')
            away_team = game.get('away_team', 'Team B')
            fixture = f"{home_team} vs {away_team}"

            # 4. Call Kimi for the tip
            tip = kimi_tip(sport_name, pick, odds, edge)

            # Format message for this specific pick
            pick_msg = (
                f"**{i+1}.** {fixture}\n"
                f"   **Pick:** {pick} @ {odds:.2f} (Edge: {edge*100:.1f}%)\n"
                f"   **Time:** {date_str}\n"
                f"   _Kimi Tip:_ {tip}"
            )
            sport_msg.append(pick_msg)

        msg.append("\n".join(sport_msg)) # Append the entire sport block to the main message list

    # 5. Send the final compiled message using markdown for formatting
    footer = "\n\n---\n⚠️ 18+ | Gamble responsibly | begambleaware.org"
    response = "\n\n".join(msg) if msg else "No games with value picks found today."

    await update.message.reply_markdown(response + footer)


def main():
    if not TG_TOKEN:
        print("Error: TELEGRAM_TOKEN environment variable not set.")
        return

    application = Application.builder().token(TG_TOKEN).build()

    application.add_handler(CommandHandler("tips", tips))

    print("Bot polling started...")
    # The run_polling() method keeps the bot running yes0000
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
