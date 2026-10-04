"""
demo_mode.py
A zero-API-quota stand-in for llm_agent, used while GEMINI_API_KEY is
missing, empty, or you just don't want to spend quota while polishing the
UI. Covers the 4 example questions shown as quick-prompt chips in the app.

IMPORTANT: this does NOT fake the weather data. It calls the exact same
weather_tools functions the real Gemini agent would call (get_weather_
briefing, get_historical_climate, ...), which hit the free, unlimited
Open-Meteo API — so numbers are always live and real. The only thing
skipped is the Gemini call itself.

MULTILINGUAL SUPPORT IS PARTIAL BY NECESSITY: the headline sentence for
each of the 4 intents is translated below for all 10 supported languages,
but the detail bullet lines underneath (crop advisory steps, alert
messages) come from weather_tools functions that generate free-form
English text — reliably translating arbitrary generated text requires an
actual translation call (i.e. Gemini itself), which is exactly what Demo
Mode exists to avoid. Those bullets stay in English with a small, honest,
localized note explaining why, rather than silently mixing languages with
no explanation.

Turn this off (go live) by setting DEMO_MODE=false (or unset) and
providing a real GEMINI_API_KEY — no other code changes needed; main.py
picks the right module automatically.
"""

import re

import weather_tools as wt

DEMO_MODE_HINT = (
    "Demo mode is on (no Gemini API calls) — try one of the 4 suggested "
    "questions below the chat box. Turn off DEMO_MODE and add a real "
    "GEMINI_API_KEY to answer anything."
)

CROP_NAMES = {
    "en": "cotton", "hi": "कपास", "gu": "કપાસ", "mr": "कापूस", "ta": "பருத்தி",
    "te": "పత్తి", "kn": "ಹತ್ತಿ", "bn": "তুলা", "pa": "ਕਪਾਹ", "ml": "പരുത്തി",
}

DIRECTION_WORDS = {
    "en": {"warmer": "warmer", "cooler": "cooler", "same": "about the same"},
    "hi": {"warmer": "अधिक गर्म", "cooler": "ठंडा", "same": "लगभग समान"},
    "gu": {"warmer": "વધુ ગરમ", "cooler": "ઠંડો", "same": "લગભગ સરખો"},
    "mr": {"warmer": "अधिक उष्ण", "cooler": "थंड", "same": "साधारण सारखे"},
    "ta": {"warmer": "வெப்பமாக", "cooler": "குளிராக", "same": "ஏறக்குறைய சமமாக"},
    "te": {"warmer": "వేడిగా", "cooler": "చల్లగా", "same": "దాదాపు సమానంగా"},
    "kn": {"warmer": "ಬಿಸಿಯಾಗಿ", "cooler": "ತಂಪಾಗಿ", "same": "ಬಹುತೇಕ ಒಂದೇ"},
    "bn": {"warmer": "বেশি গরম", "cooler": "ঠান্ডা", "same": "প্রায় একই"},
    "pa": {"warmer": "ਵੱਧ ਗਰਮ", "cooler": "ਠੰਡਾ", "same": "ਲਗਭਗ ਇੱਕੋ ਜਿਹਾ"},
    "ml": {"warmer": "ചൂടേറിയത്", "cooler": "തണുപ്പുള്ളത്", "same": "ഏകദേശം സമാനം"},
}

# One note, localized, appended whenever detail bullet lines are shown in
# English under a translated headline — see module docstring for why.
ENGLISH_DETAILS_NOTE = {
    "en": None,  # nothing to explain when the whole reply is already English
    "hi": "(विवरण डेमो मोड में अंग्रेज़ी में दिखाए गए हैं।)",
    "gu": "(વિગતો ડેમો મોડમાં અંગ્રેજીમાં બતાવવામાં આવી છે.)",
    "mr": "(तपशील डेमो मोडमध्ये इंग्रजीत दाखवले आहेत.)",
    "ta": "(விவரங்கள் டெமோ முறையில் ஆங்கிலத்தில் காட்டப்படுகின்றன.)",
    "te": "(వివరాలు డెమో మోడ్‌లో ఆంగ్లంలో చూపబడ్డాయి.)",
    "kn": "(ವಿವರಗಳನ್ನು ಡೆಮೊ ಮೋಡ್‌ನಲ್ಲಿ ಇಂಗ್ಲಿಷ್‌ನಲ್ಲಿ ತೋರಿಸಲಾಗಿದೆ.)",
    "bn": "(বিস্তারিত ডেমো মোডে ইংরেজিতে দেখানো হয়েছে।)",
    "pa": "(ਵੇਰਵੇ ਡੈਮੋ ਮੋਡ ਵਿੱਚ ਅੰਗਰੇਜ਼ੀ ਵਿੱਚ ਦਿਖਾਏ ਗਏ ਹਨ।)",
    "ml": "(വിശദാംശങ്ങൾ ഡെമോ മോഡിൽ ഇംഗ്ലീഷിൽ കാണിച്ചിരിക്കുന്നു.)",
}

RAIN_TEMPLATE = {
    "en": "Over the next 5 days in {place}, about {total_rain} mm of rain is expected ({days} rainy day(s)).",
    "hi": "{place} में अगले 5 दिनों में लगभग {total_rain} मिमी बारिश होने की संभावना है ({days} दिन बारिश के साथ)।",
    "gu": "{place} માં આગામી 5 દિવસમાં લગભગ {total_rain} મીમી વરસાદ થવાની શક્યતા છે ({days} વરસાદી દિવસો).",
    "mr": "{place} मध्ये पुढील 5 दिवसांत सुमारे {total_rain} मिमी पाऊस पडण्याची शक्यता आहे ({days} पावसाळी दिवस).",
    "ta": "{place} இல் அடுத்த 5 நாட்களில் சுமார் {total_rain} மிமீ மழை பெய்யும் என எதிர்பார்க்கப்படுகிறது ({days} மழை நாட்கள்).",
    "te": "{place} లో వచ్చే 5 రోజుల్లో సుమారు {total_rain} మిమీ వర్షం కురిసే అవకాశం ఉంది ({days} వర్ష దినాలు).",
    "kn": "{place} ನಲ್ಲಿ ಮುಂದಿನ 5 ದಿನಗಳಲ್ಲಿ ಸುಮಾರು {total_rain} ಮಿಮೀ ಮಳೆ ನಿರೀಕ್ಷಿಸಲಾಗಿದೆ ({days} ಮಳೆಯ ದಿನಗಳು).",
    "bn": "{place}-এ আগামী ৫ দিনে প্রায় {total_rain} মিমি বৃষ্টি হতে পারে ({days} দিন বৃষ্টি সহ)।",
    "pa": "{place} ਵਿੱਚ ਅਗਲੇ 5 ਦਿਨਾਂ ਵਿੱਚ ਲਗਭਗ {total_rain} ਮਿਮੀ ਮੀਂਹ ਪੈਣ ਦੀ ਸੰਭਾਵਨਾ ਹੈ ({days} ਦਿਨ ਮੀਂਹ ਵਾਲੇ)।",
    "ml": "{place} ൽ അടുത്ത 5 ദിവസത്തിനുള്ളിൽ ഏകദേശം {total_rain} മില്ലിമീറ്റർ മഴ പ്രതീക്ഷിക്കുന്നു ({days} മഴ ദിവസങ്ങൾ).",
}

IRRIGATE_TEMPLATE = {
    "en": "For your {crop} field near {place}, about {rain3} mm of rain is expected over the next 3 days.",
    "hi": "{place} के पास आपके {crop} खेत के लिए, अगले 3 दिनों में लगभग {rain3} मिमी बारिश होने की उम्मीद है।",
    "gu": "{place} નજીક તમારા {crop} ખેતર માટે, આગામી 3 દિવસમાં લગભગ {rain3} મીમી વરસાદની અપેક્ષા છે.",
    "mr": "{place} जवळील तुमच्या {crop} शेतासाठी, पुढील 3 दिवसांत सुमारे {rain3} मिमी पावसाची अपेक्षा आहे.",
    "ta": "{place} அருகிலுள்ள உங்கள் {crop} வயலுக்கு, அடுத்த 3 நாட்களில் சுமார் {rain3} மிமீ மழை எதிர்பார்க்கப்படுகிறது.",
    "te": "{place} సమీపంలోని మీ {crop} పొలానికి, రాబోయే 3 రోజుల్లో సుమారు {rain3} మిమీ వర్షం పడే అవకాశం ఉంది.",
    "kn": "{place} ಬಳಿಯ ನಿಮ್ಮ {crop} ಹೊಲಕ್ಕೆ, ಮುಂದಿನ 3 ದಿನಗಳಲ್ಲಿ ಸುಮಾರು {rain3} ಮಿಮೀ ಮಳೆ ನಿರೀಕ್ಷಿಸಲಾಗಿದೆ.",
    "bn": "{place}-এর কাছে আপনার {crop} ক্ষেতে, আগামী ৩ দিনে প্রায় {rain3} মিমি বৃষ্টি হতে পারে।",
    "pa": "{place} ਦੇ ਨੇੜੇ ਤੁਹਾਡੇ {crop} ਖੇਤ ਲਈ, ਅਗਲੇ 3 ਦਿਨਾਂ ਵਿੱਚ ਲਗਭਗ {rain3} ਮਿਮੀ ਮੀਂਹ ਦੀ ਉਮੀਦ ਹੈ।",
    "ml": "{place} ന് സമീപമുള്ള നിങ്ങളുടെ {crop} വയലിന്, അടുത്ത 3 ദിവസത്തിനുള്ളിൽ ഏകദേശം {rain3} മില്ലിമീറ്റർ മഴ പ്രതീക്ഷിക്കുന്നു.",
}

STORM_NONE_TEMPLATE = {
    "en": "No severe-weather alerts are currently flagged for {place} in the next 7 days — conditions look routine.",
    "hi": "{place} के लिए अगले 7 दिनों में फिलहाल कोई गंभीर मौसम चेतावनी नहीं है — स्थिति सामान्य लग रही है।",
    "gu": "{place} માટે આગામી 7 દિવસમાં હાલમાં કોઈ ગંભીર હવામાન ચેતવણી નથી — સ્થિતિ સામાન્ય લાગે છે.",
    "mr": "{place} साठी पुढील 7 दिवसांत सध्या कोणताही गंभीर हवामान इशारा नाही — परिस्थिती सामान्य दिसते.",
    "ta": "{place} க்கு அடுத்த 7 நாட்களில் தற்போது கடுமையான வானிலை எச்சரிக்கை எதுவும் இல்லை — நிலைமை சாதாரணமாக உள்ளது.",
    "te": "{place} కు రాబోయే 7 రోజుల్లో ప్రస్తుతం తీవ్రమైన వాతావరణ హెచ్చరికలు ఏవీ లేవు — పరిస్థితులు సాధారణంగా ఉన్నాయి.",
    "kn": "{place} ಗೆ ಮುಂದಿನ 7 ದಿನಗಳಲ್ಲಿ ಪ್ರಸ್ತುತ ಯಾವುದೇ ತೀವ್ರ ಹವಾಮಾನ ಎಚ್ಚರಿಕೆ ಇಲ್ಲ — ಪರಿಸ್ಥಿತಿ ಸಾಮಾನ್ಯವಾಗಿದೆ.",
    "bn": "{place}-এর জন্য আগামী ৭ দিনে বর্তমানে কোনো গুরুতর আবহাওয়া সতর্কতা নেই — পরিস্থিতি স্বাভাবিক মনে হচ্ছে।",
    "pa": "{place} ਲਈ ਅਗਲੇ 7 ਦਿਨਾਂ ਵਿੱਚ ਫਿਲਹਾਲ ਕੋਈ ਗੰਭੀਰ ਮੌਸਮ ਚੇਤਾਵਨੀ ਨਹੀਂ ਹੈ — ਹਾਲਾਤ ਆਮ ਲੱਗਦੇ ਹਨ।",
    "ml": "{place} ന് അടുത്ത 7 ദിവസത്തിനുള്ളിൽ നിലവിൽ ഗുരുതരമായ കാലാവസ്ഥാ മുന്നറിയിപ്പുകൾ ഇല്ല — സ്ഥിതി സാധാരണമായി തോന്നുന്നു.",
}

STORM_ACTIVE_TEMPLATE = {
    "en": "Active alerts for {place}:",
    "hi": "{place} के लिए सक्रिय चेतावनियाँ:",
    "gu": "{place} માટે સક્રિય ચેતવણીઓ:",
    "mr": "{place} साठी सक्रिय इशारे:",
    "ta": "{place} க்கான செயலில் உள்ள எச்சரிக்கைகள்:",
    "te": "{place} కోసం క్రియాశీల హెచ్చరికలు:",
    "kn": "{place} ಗಾಗಿ ಸಕ್ರಿಯ ಎಚ್ಚರಿಕೆಗಳು:",
    "bn": "{place}-এর জন্য সক্রিয় সতর্কতা:",
    "pa": "{place} ਲਈ ਸਰਗਰਮ ਚੇਤਾਵਨੀਆਂ:",
    "ml": "{place} നുള്ള സജീവ മുന്നറിയിപ്പുകൾ:",
}

TREND_TEMPLATE = {
    "en": "Comparing April-June averages: {y1} averaged {avg1}°C max, {y2} averaged {avg2}°C max — this summer has been {direction} by about {diff}°C.",
    "hi": "अप्रैल-जून के औसत की तुलना: {y1} में औसत अधिकतम {avg1}°C था, {y2} में औसत अधिकतम {avg2}°C — इस गर्मी में लगभग {diff}°C {direction} रहा है।",
    "gu": "એપ્રિલ-જૂનની સરેરાશની સરખામણી: {y1} માં સરેરાશ મહત્તમ {avg1}°C હતું, {y2} માં સરેરાશ મહત્તમ {avg2}°C — આ ઉનાળો લગભગ {diff}°C {direction} રહ્યો છે.",
    "mr": "एप्रिल-जूनच्या सरासरीची तुलना: {y1} मध्ये सरासरी कमाल {avg1}°C होते, {y2} मध्ये सरासरी कमाल {avg2}°C — या उन्हाळ्यात सुमारे {diff}°C {direction} आहे.",
    "ta": "ஏப்ரல்-ஜூன் சராசரிகளை ஒப்பிடுகையில்: {y1} இல் சராசரி அதிகபட்சம் {avg1}°C, {y2} இல் சராசரி அதிகபட்சம் {avg2}°C — இந்த கோடை சுமார் {diff}°C {direction} உள்ளது.",
    "te": "ఏప్రిల్-జూన్ సగటులను పోలుస్తే: {y1} లో సగటు గరిష్టం {avg1}°C, {y2} లో సగటు గరిష్టం {avg2}°C — ఈ వేసవి సుమారు {diff}°C {direction} ఉంది.",
    "kn": "ಏಪ್ರಿಲ್-ಜೂನ್ ಸರಾಸರಿಗಳನ್ನು ಹೋಲಿಸಿದರೆ: {y1} ರಲ್ಲಿ ಸರಾಸರಿ ಗರಿಷ್ಠ {avg1}°C, {y2} ರಲ್ಲಿ ಸರಾಸರಿ ಗರಿಷ್ಠ {avg2}°C — ಈ ಬೇಸಿಗೆ ಸುಮಾರು {diff}°C {direction} ಆಗಿದೆ.",
    "bn": "এপ্রিল-জুন গড় তুলনা: {y1} সালে গড় সর্বোচ্চ {avg1}°C, {y2} সালে গড় সর্বোচ্চ {avg2}°C — এই গ্রীষ্মে প্রায় {diff}°C {direction}।",
    "pa": "ਅਪ੍ਰੈਲ-ਜੂਨ ਔਸਤਾਂ ਦੀ ਤੁਲਨਾ: {y1} ਵਿੱਚ ਔਸਤ ਵੱਧ ਤੋਂ ਵੱਧ {avg1}°C ਸੀ, {y2} ਵਿੱਚ ਔਸਤ ਵੱਧ ਤੋਂ ਵੱਧ {avg2}°C — ਇਸ ਗਰਮੀ ਵਿੱਚ ਲਗਭਗ {diff}°C {direction} ਰਿਹਾ ਹੈ।",
    "ml": "ഏപ്രിൽ-ജൂൺ ശരാശരി താരതമ്യം: {y1} ൽ ശരാശരി പരമാവധി {avg1}°C, {y2} ൽ ശരാശരി പരമാവധി {avg2}°C — ഈ വേനൽക്കാലം ഏകദേശം {diff}°C {direction} ആണ്.",
}

NOT_FOUND_TEMPLATE = {
    "en": "I couldn't find a place called '{place}'.",
    "hi": "मुझे '{place}' नाम की जगह नहीं मिली।",
    "gu": "મને '{place}' નામનું સ્થળ મળ્યું નથી.",
    "mr": "मला '{place}' नावाचे ठिकाण सापडले नाही.",
    "ta": "'{place}' என்ற இடத்தை என்னால் கண்டுபிடிக்க முடியவில்லை.",
    "te": "'{place}' అనే స్థలం నాకు దొరకలేదు.",
    "kn": "'{place}' ಎಂಬ ಸ್ಥಳ ನನಗೆ ಸಿಗಲಿಲ್ಲ.",
    "bn": "আমি '{place}' নামের জায়গাটি খুঁজে পাইনি।",
    "pa": "ਮੈਨੂੰ '{place}' ਨਾਮ ਦੀ ਥਾਂ ਨਹੀਂ ਮਿਲੀ।",
    "ml": "'{place}' എന്ന സ്ഥലം എനിക്ക് കണ്ടെത്താനായില്ല.",
}


def _t(table: dict, language: str, **kwargs) -> str:
    template = table.get(language, table["en"])
    return template.format(**kwargs)


def _with_english_details(headline: str, lines: list, language: str) -> str:
    body = headline
    if lines:
        body += "\n\n" + "\n".join(f"- {l}" for l in lines)
    note = ENGLISH_DETAILS_NOTE.get(language)
    if note and lines:
        body += "\n\n" + note
    return body


def _fmt_forecast_line(daily: dict, idx: int) -> str:
    dates = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    rain = daily.get("precipitation_sum", [])
    prob = daily.get("precipitation_probability_max", [])
    if idx >= len(dates):
        return ""
    return (f"{dates[idx]}: up to {tmax[idx]}°C, "
            f"{rain[idx]} mm rain ({prob[idx] if idx < len(prob) else '?'}% chance)")


async def _rain_this_week(place: str, language: str) -> str:
    briefing = await wt.get_weather_briefing(place, days=5)
    if "error" in briefing:
        return _t(NOT_FOUND_TEMPLATE, language, place=place)
    loc = briefing["location"]
    place_label = f"{loc['name']}, {loc.get('admin1', '')}".rstrip(", ")
    daily = briefing["forecast"].get("daily", {})
    total_rain = round(sum(v for v in daily.get("precipitation_sum", []) if v is not None), 1)
    days_with_rain = sum(1 for v in daily.get("precipitation_sum", []) if v and v > 0.5)
    lines = [_fmt_forecast_line(daily, i) for i in range(min(5, len(daily.get("time", []))))]
    lines = [l for l in lines if l]
    headline = _t(RAIN_TEMPLATE, language, place=place_label, total_rain=total_rain, days=days_with_rain)
    if briefing.get("alerts"):
        lines = [f"Note: {briefing['alerts'][0]['message']}"] + lines
    return _with_english_details(headline, lines, language)


async def _irrigate_advisory(place: str, crop: str, language: str) -> str:
    briefing = await wt.get_weather_briefing(place, days=5, crop=crop)
    if "error" in briefing:
        return _t(NOT_FOUND_TEMPLATE, language, place=place)
    loc = briefing["location"]
    place_label = f"{loc['name']}, {loc.get('admin1', '')}".rstrip(", ")
    advisory = briefing.get("advisory", {})
    lines = advisory.get("advisory", [])
    crop_label = CROP_NAMES.get(language, crop)
    headline = _t(IRRIGATE_TEMPLATE, language, place=place_label, crop=crop_label,
                  rain3=advisory.get("rain_next_3_days_mm", 0))
    return _with_english_details(headline, lines, language)


async def _storm_warning(place: str, language: str) -> str:
    briefing = await wt.get_weather_briefing(place, days=7)
    if "error" in briefing:
        return _t(NOT_FOUND_TEMPLATE, language, place=place)
    loc = briefing["location"]
    place_label = f"{loc['name']}, {loc.get('admin1', '')}".rstrip(", ")
    alerts = briefing.get("alerts", [])
    if not alerts:
        return _t(STORM_NONE_TEMPLATE, language, place=place_label)
    headline = _t(STORM_ACTIVE_TEMPLATE, language, place=place_label)
    lines = [f"{a['date']}: {a['message']} (severity: {a['severity']})" for a in alerts]
    return _with_english_details(headline, lines, language)


async def _delhi_temperature_trend(language: str) -> str:
    loc = await wt.geocode_location("Delhi")
    if "error" in loc:
        return _t(NOT_FOUND_TEMPLATE, language, place="Delhi")
    history = await wt.get_historical_climate(loc["lat"], loc["lon"], days_back=420)
    daily = history.get("daily", {})
    dates = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])

    def summer_avg(year):
        vals = [t for d, t in zip(dates, tmax) if d.startswith(year) and d[5:7] in ("04", "05", "06") and t is not None]
        return (sum(vals) / len(vals)) if vals else None

    years = sorted({d[:4] for d in dates})
    if len(years) < 2:
        return "Not enough historical data was returned to compare summers."
    prev_avg, this_avg = summer_avg(years[-2]), summer_avg(years[-1])
    if prev_avg is None or this_avg is None:
        return "Not enough April-June data was returned for both years to compare."
    direction_key = "warmer" if this_avg > prev_avg else ("cooler" if this_avg < prev_avg else "same")
    direction = DIRECTION_WORDS.get(language, DIRECTION_WORDS["en"])[direction_key]
    diff = round(abs(this_avg - prev_avg), 1)
    return _t(TREND_TEMPLATE, language, y1=years[-2], y2=years[-1],
              avg1=round(prev_avg, 1), avg2=round(this_avg, 1), direction=direction, diff=diff)


# (message pattern, handler) — checked in order, first match wins. Handlers
# take `language` so they can return a translated headline.
_INTENTS = [
    (re.compile(r"ahmedabad", re.I), lambda language: _rain_this_week("Ahmedabad", language)),
    (re.compile(r"nagpur", re.I), lambda language: _irrigate_advisory("Nagpur", "cotton", language)),
    (re.compile(r"bhubaneswar", re.I), lambda language: _storm_warning("Bhubaneswar", language)),
    (re.compile(r"delhi", re.I), lambda language: _delhi_temperature_trend(language)),
]


async def demo_reply(user_message: str, language: str = "en") -> dict:
    """Best-effort canned reply for one of the 4 example questions, using
    live (not hardcoded) weather data, with a translated headline for any
    of the 10 supported languages. Returns the same shape as
    llm_agent.chat(): {"reply": str, "trace": []}.
    """
    for pattern, handler in _INTENTS:
        if pattern.search(user_message):
            try:
                text = await handler(language)
            except Exception as exc:
                text = f"(demo mode) That lookup failed: {exc}"
            return {"reply": text, "trace": []}
    return {"reply": DEMO_MODE_HINT, "trace": []}


async def demo_reply_stream(user_message: str, language: str = "en"):
    """Streaming-shaped version of demo_reply, for /chat/stream — yields the
    whole answer as one token (no real streaming needed for canned demo
    content) then a done event, matching llm_agent.chat_stream()'s protocol.
    """
    result = await demo_reply(user_message, language)
    yield {"type": "token", "text": result["reply"]}
    yield {"type": "done", "trace": []}
