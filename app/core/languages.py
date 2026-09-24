"""Canonical language registry for the Bharat Vistaar voice backend."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    code: str
    name: str
    sarvam_code: str
    recording_disclaimer: str
    closing_message: str
    hold_message: str
    delay_message: str


LANGUAGES: dict[str, LanguageSpec] = {
    "en": LanguageSpec(
        "en", "English", "en-IN",
        "This call is being recorded for training and quality improvement. Your information will remain secure.",
        "You can call this helpline anytime for weather, crop-related advice, or schemes. Thank you for calling Bharat Vistaar, a service of the Union Ministry of Agriculture. Wishing you a good harvest and a successful season.",
        "Information pertaining to your question is being fetched. Please wait.",
        "We are sorry for the wait. Please stay with us.",
    ),
    "hi": LanguageSpec(
        "hi", "Hindi", "hi-IN",
        "यह कॉल प्रशिक्षण और गुणवत्ता सुधार हेतु रिकॉर्ड की जा रही है। आपकी जानकारी सुरक्षित रहेगी।",
        "मौसम, फसल संबंधी सलाह या योजनाओं के लिए आप किसी भी समय इस हेल्पलाइन पर कॉल कर सकते हैं। केंद्रीय कृषि मंत्रालय की सेवा भारत विस्तार को कॉल करने के लिए धन्यवाद। आपको अच्छी फसल और सफल मौसम की शुभकामनाएं।",
        "आपके प्रश्न से संबंधित जानकारी एकत्रित की जा रही है। कृपया प्रतीक्षा करें।",
        "प्रतीक्षा के लिए खेद है। कृपया हमारे साथ बने रहें।",
    ),
    "od": LanguageSpec(
        "od", "Odia", "od-IN",
        "ଏହି କଲ୍ ପ୍ରଶିକ୍ଷଣ ଏବଂ ଗୁଣବତ୍ତା ଉନ୍ନତି ପାଇଁ ରେକର୍ଡ କରାଯାଉଛି। ଆପଣଙ୍କ ସୂଚନା ସୁରକ୍ଷିତ ରହିବ।",
        "ଆପଣ ପାଣିପାଗ, ଫସଲ ସମ୍ବନ୍ଧୀୟ ପରାମର୍ଶ କିମ୍ବା ଯୋଜନା ପାଇଁ ଯେକୌଣସି ସମୟରେ ଏହି ହେଲ୍ପଲାଇନକୁ କଲ୍ କରିପାରିବେ। କେନ୍ଦ୍ର କୃଷି ମନ୍ତ୍ରଣାଳୟର ଏକ ସେବା, ଭାରତ ବିସ୍ତାରକୁ କଲ୍ କରିଥିବାରୁ ଧନ୍ୟବାଦ। ଆପଣଙ୍କ ଫସଲ ଭଲ ହେଉ ଏବଂ ଏହି ଋତୁ ସଫଳ ହେଉ।",
        "ଆପଣଙ୍କ ପ୍ରଶ୍ନ ସମ୍ବନ୍ଧୀୟ ସୂଚନା ସଂଗ୍ରହ କରାଯାଉଛି। ଦୟାକରି ଅପେକ୍ଷା କରନ୍ତୁ।",
        "ଅପେକ୍ଷା ପାଇଁ ଦୁଃଖିତ। ଦୟାକରି ଆମ ସହିତ ରୁହନ୍ତୁ।",
    ),
    "pa": LanguageSpec(
        "pa", "Punjabi", "pa-IN",
        "ਇਹ ਕਾਲ ਸਿਖਲਾਈ ਅਤੇ ਗੁਣਵੱਤਾ ਵਿੱਚ ਸੁਧਾਰ ਲਈ ਰਿਕਾਰਡ ਕੀਤੀ ਜਾ ਰਹੀ ਹੈ। ਤੁਹਾਡੀ ਜਾਣਕਾਰੀ ਸੁਰੱਖਿਅਤ ਰਹੇਗੀ।",
        "ਤੁਸੀਂ ਮੌਸਮ, ਫ਼ਸਲ ਸੰਬੰਧੀ ਸਲਾਹ ਜਾਂ ਸਕੀਮਾਂ ਲਈ ਕਿਸੇ ਵੀ ਸਮੇਂ ਇਸ ਹੈਲਪਲਾਈਨ ਉੱਤੇ ਕਾਲ ਕਰ ਸਕਦੇ ਹੋ। ਕੇਂਦਰੀ ਖੇਤੀਬਾੜੀ ਮੰਤਰਾਲੇ ਦੀ ਇੱਕ ਸੇਵਾ, ਭਾਰਤ ਵਿਸਤਾਰ ਨੂੰ ਕਾਲ ਕਰਨ ਲਈ ਤੁਹਾਡਾ ਧੰਨਵਾਦ। ਤੁਹਾਡੀ ਫ਼ਸਲ ਚੰਗੀ ਹੋਵੇ ਅਤੇ ਇਹ ਸੀਜ਼ਨ ਸਫਲ ਰਹੇ, ਇਹੀ ਸ਼ੁਭਕਾਮਨਾ।",
        "ਤੁਹਾਡੇ ਸਵਾਲ ਨਾਲ ਸਬੰਧਤ ਜਾਣਕਾਰੀ ਇਕੱਠੀ ਕੀਤੀ ਜਾ ਰਹੀ ਹੈ। ਕਿਰਪਾ ਕਰਕੇ ਉਡੀਕ ਕਰੋ।",
        "ਉਡੀਕ ਲਈ ਮੁਆਫ਼ੀ ਚਾਹੁੰਦੇ ਹਾਂ। ਕਿਰਪਾ ਕਰਕੇ ਸਾਡੇ ਨਾਲ ਬਣੇ ਰਹੋ।",
    ),
    "ta": LanguageSpec(
        "ta", "Tamil", "ta-IN",
        "இந்த அழைப்பு பயிற்சி மற்றும் தர மேம்பாட்டிற்காக பதிவு செய்யப்படுகிறது. உங்கள் தகவல்கள் பாதுகாக்கப்படும்.",
        "வானிலை, பயிர் தொடர்பான ஆலோசனை அல்லது திட்டங்களுக்கு நீங்கள் எப்போது வேண்டுமானாலும் இந்த உதவி எண்ணை அழைக்கலாம். மத்திய விவசாய அமைச்சகத்தின் சேவை பாரத் விஸ்தாரை அழைத்ததற்கு நன்றி. உங்களுக்கு நல்ல அறுவடையும் வெற்றிகரமான பருவமும் வாழ்த்துகிறேன்.",
        "உங்கள் கேள்வி தொடர்பான தகவல்கள் சேகரிக்கப்படுகின்றன. தயவுசெய்து காத்திருங்கள்.",
        "காத்திருப்புக்கு வருந்துகிறோம். தயவுசெய்து எங்களுடன் இணைந்திருங்கள்.",
    ),
    "te": LanguageSpec(
        "te", "Telugu", "te-IN",
        "ఈ కాల్ శిక్షణ మరియు నాణ్యత మెరుగుదల కోసం రికార్డ్ చేయబడుతోంది. మీ వ్యక్తిగత సమాచారం సురక్షితంగా ఉంటుంది.",
        "వాతావరణం, పంట సంబంధిత సలహా లేదా పథకాల కోసం మీరు ఎప్పుడైనా ఈ హెల్ప్‌లైన్‌కు కాల్ చేయవచ్చు. కేంద్ర వ్యవసాయ మంత్రిత్వ శాఖ సేవ భారత్ విస్తార్‌కు కాల్ చేసినందుకు ధన్యవాదాలు. మీకు మంచి పంట మరియు విజయవంతమైన సీజన్ కావాలని కోరుకుంటున్నాను.",
        "మీ ప్రశ్నకు సంబంధించిన సమాచారం సేకరిస్తున్నాము. దయచేసి వేచి ఉండండి.",
        "వేచి ఉంచినందుకు క్షమించండి. దయచేసి మాతో ఉండండి.",
    ),
    "kn": LanguageSpec(
        "kn", "Kannada", "kn-IN",
        "ಈ ಕರೆಯನ್ನು ತರಬೇತಿ ಮತ್ತು ಗುಣಮಟ್ಟ ಸುಧಾರಣೆಗಾಗಿ ರೆಕಾರ್ಡ್ ಮಾಡಲಾಗುತ್ತಿದೆ. ನಿಮ್ಮ ಮಾಹಿತಿ ಸುರಕ್ಷಿತವಾಗಿರುತ್ತದೆ.",
        "ಹವಾಮಾನ, ಬೆಳೆ ಸಂಬಂಧಿತ ಸಲಹೆ ಅಥವಾ ಯೋಜನೆಗಳಿಗಾಗಿ ನೀವು ಯಾವಾಗ ಬೇಕಾದರೂ ಈ ಹೆಲ್ಪ್‌ಲೈನ್‌ಗೆ ಕರೆ ಮಾಡಬಹುದು. ಕೇಂದ್ರ ಕೃಷಿ ಸಚಿವಾಲಯದ ಸೇವೆ ಭಾರತ್ ವಿಸ್ತಾರ್‌ಗೆ ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದ. ನಿಮಗೆ ಒಳ್ಳೆಯ ಬೆಳೆ ಮತ್ತು ಯಶಸ್ವಿ ಋತುವಿನ ಶುಭಾಶಯಗಳು.",
        "ನಿಮ್ಮ ಪ್ರಶ್ನೆಗೆ ಸಂಬಂಧಿಸಿದ ಮಾಹಿತಿಯನ್ನು ಸಂಗ್ರಹಿಸಲಾಗುತ್ತಿದೆ. ದಯವಿಟ್ಟು ನಿರೀಕ್ಷಿಸಿ.",
        "ಕಾಯಿಸಿದ್ದಕ್ಕೆ ಕ್ಷಮಿಸಿ. ದಯವಿಟ್ಟು ನಮ್ಮೊಂದಿಗೆ ಇರಿ.",
    ),
    "ml": LanguageSpec(
        "ml", "Malayalam", "ml-IN",
        "ഈ കോൾ പരിശീലനത്തിനും ഗുണനിലവാര മെച്ചപ്പെടുത്തലിനുമായി റെക്കോർഡ് ചെയ്യുന്നു. നിങ്ങളുടെ വ്യക്തിഗത വിവരങ്ങൾ സുരക്ഷിതമായിരിക്കും.",
        "കാലാവസ്ഥ, വിള ഉപദേശം, അല്ലെങ്കിൽ പദ്ധതികൾക്കായി നിങ്ങൾ ഈ ഹെൽപ്‌ലൈനിൽ ഏത് സമയവും വിളിക്കാം. കേന്ദ്ര കൃഷി മന്ത്രാലയത്തിന്റെ സേവനം ഭാരത് വിസ്താർ വിളിച്ചതിന് നന്ദി. നിങ്ങൾക്ക് നല്ല വിളയും വിജയകരമായ ഋതുവും ആശംസിക്കുന്നു.",
        "നിങ്ങളുടെ ചോദ്യവുമായി ബന്ധപ്പെട്ട വിവരങ്ങൾ ശേഖരിക്കുന്നു. ദയവായി കാത്തിരിക്കൂ.",
        "കാത്തിരിപ്പിന് ക്ഷമിക്കണം. ദയവായി ഞങ്ങളോടൊപ്പം തുടരൂ.",
    ),
    "gu": LanguageSpec(
        "gu", "Gujarati", "gu-IN",
        "આ કૉલ પ્રશિક્ષણ અને ગુણવત્તા સુધારણા માટે રેકૉર્ડ કરવામાં આવી રહ્યો છે. તમારી માહિતી સુરક્ષિત રહેશે.",
        "હવામાન, પાક સંબંધિત સલાહ અથવા યોજનાઓ માટે તમે ગમે ત્યારે આ હેલ્પલાઇન પર કૉલ કરી શકો છો. કેન્દ્રીય કૃષિ મંત્રાલયની સેવા ભારત વિસ્તારને કૉલ કરવા બદલ આભાર. તમને સારો પાક અને સફળ મોસમ મળે તેવી શુભકામનાઓ.",
        "તમારા પ્રશ્ન સંબંધિત માહિતી એકત્રિત કરવામાં આવી રહી છે. કૃપા કરીને રાહ જુઓ.",
        "રાહ જોવડાવવા બદલ દિલગીર છીએ. કૃપા કરીને અમારી સાથે જોડાયેલા રહો.",
    ),
    "mr": LanguageSpec(
        "mr", "Marathi", "mr-IN",
        "हा कॉल प्रशिक्षण आणि गुणवत्ता सुधारणेसाठी रेकॉर्ड केला जात आहे. तुमची माहिती सुरक्षित राहील.",
        "हवामान, पीक संबंधी सल्ला किंवा योजनांसाठी तुम्ही कधीही या हेल्पलाइनवर कॉल करू शकता. केंद्रीय कृषी मंत्रालयाची सेवा भारत विस्तारला कॉल केल्याबद्दल धन्यवाद. तुम्हाला चांगले पीक आणि यशस्वी हंगामासाठी शुभेच्छा.",
        "तुमच्या प्रश्नाशी संबंधित माहिती गोळा केली जात आहे. कृपया प्रतीक्षा करा.",
        "प्रतीक्षेबद्दल क्षमस्व. कृपया आमच्यासोबत रहा.",
    ),
    "bn": LanguageSpec(
        "bn", "Bengali", "bn-IN",
        "এই কলটি প্রশিক্ষণ এবং গুণমান উন্নয়নের জন্য রেকর্ড করা হচ্ছে। আপনার তথ্য সুরক্ষিত থাকবে।",
        "আবহাওয়া, ফসল সম্পর্কিত পরামর্শ বা প্রকল্পের জন্য আপনি যেকোনো সময় এই হেল্পলাইনে কল করতে পারেন। কেন্দ্রীয় কৃষি মন্ত্রকের পরিষেবা ভারত বিস্তারে কল করার জন্য ধন্যবাদ। আপনাকে ভালো ফসল ও সফল মৌসুমের শুভকামনা।",
        "আপনার প্রশ্ন সম্পর্কিত তথ্য সংগ্রহ করা হচ্ছে। অনুগ্রহ করে অপেক্ষা করুন।",
        "অপেক্ষার জন্য দুঃখিত। অনুগ্রহ করে আমাদের সঙ্গে থাকুন।",
    ),
}

SUPPORTED_LANGUAGES = {code: spec.name for code, spec in LANGUAGES.items()}
SUPPORTED_LANGUAGE_CODES = frozenset(LANGUAGES)
DEFAULT_LANGUAGE = "hi"
LANGUAGE_ALIASES = {"or": "od"}
ISO_TO_INTERNAL_LANGUAGE = {
    "en": "en",
    "hi": "hi",
    "od": "od",
    "pa": "pa",
    "ta": "ta",
    "te": "te",
    "kn": "kn",
    "ml": "ml",
    "gu": "gu",
    "mr": "mr",
    "bn": "bn",
}
INTERNAL_TO_ISO_LANGUAGE = {
    internal: iso for iso, internal in ISO_TO_INTERNAL_LANGUAGE.items()
}
SUPPORTED_ISO_LANGUAGE_CODES = frozenset(ISO_TO_INTERNAL_LANGUAGE)


def normalize_language(lang: str | None, *, fallback: str = DEFAULT_LANGUAGE) -> str:
    if not lang:
        return fallback
    raw = lang.strip().lower()
    code = LANGUAGE_ALIASES.get(raw, raw)
    return code if code in LANGUAGES else fallback


def parse_iso_language_header(lang: str | None) -> str:
    """Validate a Sarvam X-Language value and return its internal code."""
    raw = (lang or "").strip().lower()
    try:
        return ISO_TO_INTERNAL_LANGUAGE[raw]
    except KeyError as exc:
        supported = ", ".join(sorted(SUPPORTED_ISO_LANGUAGE_CODES))
        raise ValueError(
            f"X-Language must be one of these supported codes: {supported}"
        ) from exc


def iso_language_code(lang: str | None) -> str:
    """Return the public Sarvam language code for an internal language code."""
    internal = normalize_language(lang, fallback="")
    try:
        return INTERNAL_TO_ISO_LANGUAGE[internal]
    except KeyError as exc:
        raise ValueError(f"Unsupported internal language code: {lang!r}") from exc


def is_supported(lang: str | None) -> bool:
    return bool(lang) and normalize_language(lang, fallback="") in LANGUAGES


def get_language(lang: str | None) -> LanguageSpec:
    return LANGUAGES[normalize_language(lang)]


def sarvam_language_code(lang: str | None) -> str:
    return get_language(lang).sarvam_code
