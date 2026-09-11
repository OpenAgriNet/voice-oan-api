# Bharat Vistaar voice call-control messages

This document records the call-ending messages currently prescribed by the
`bh-voice-prod` backend prompts and the repeat/error messages configured on the
Sarvam side.

## Call-ending contract

The backend does not call a Sarvam hang-up API. The LLM generates the closing
text and sets `end_interaction` in its structured output. The backend forwards
the final result to Sarvam in this form:

```json
{
  "audio": "<closing message>",
  "end_interaction": true,
  "language": "<language code>"
}
```

Partial streaming responses always use `end_interaction: false`. The final
response may use `true`. Each prompt currently tells the LLM to set it to
`true` only after `submit_feedback` has been called and the prescribed closing
line has been spoken.

The `closing_message` values in `app/core/languages.py` are currently unused.
The effective closing copy comes from the language prompts.

## Prescribed closing statements

### English (`en`)

> Thank you for calling the Bharat Vistaar Helpline, a service of the Ministry of Agriculture and Farmers Welfare. I hope the information was useful for you. You can call this helpline anytime for weather, crop advice or schemes. Wishing you a good crop and a successful season.

### Hindi (`hi`)

> मौसम, फसल संबंधी सलाह या योजनाओं के लिए आप किसी भी समय इस हेल्पलाइन पर कॉल कर सकते हैं। केंद्रीय कृषि मंत्रालय की सेवा भारत विस्तार को कॉल करने के लिए धन्यवाद। आपको अच्छी फसल और सफल मौसम की शुभकामनाएं।

### Odia (`od`)

> ଭାରତ ବିସ୍ତାର ହେଲ୍ପଲାଇନକୁ ଫୋନ୍ କରିଥିବାରୁ ଧନ୍ୟବାଦ, ଏହା କୃଷି ଓ କୃଷକ କଲ୍ୟାଣ ମନ୍ତ୍ରଣାଳୟର ଏକ ସେବା। ମୁଁ ଆଶା କରୁଛି ସୂଚନା ଆପଣଙ୍କ ପାଇଁ ଉପଯୋଗୀ ହୋଇଥିବ। ପାଣିପାଗ, ଫସଲ ପରାମର୍ଶ କିମ୍ବା ଯୋଜନା ପାଇଁ ଆପଣ ଯେକୌଣସି ସମୟରେ ଏହି ହେଲ୍ପଲାଇନକୁ ଫୋନ୍ କରିପାରିବେ। ଆପଣଙ୍କୁ ଏକ ଭଲ ଫସଲ ଏବଂ ଏକ ସଫଳ ଋତୁ ପାଇଁ ଶୁଭେଚ୍ଛା।

### Punjabi (`pa`)

> ਖੇਤੀਬਾੜੀ ਅਤੇ ਕਿਸਾਨ ਭਲਾਈ ਮੰਤਰਾਲੇ ਦੀ ਸੇਵਾ, ਭਾਰਤ ਵਿਸਤਾਰ ਹੈਲਪਲਾਈਨ ਉੱਤੇ ਫ਼ੋਨ ਕਰਨ ਲਈ ਧੰਨਵਾਦ। ਮੈਨੂੰ ਉਮੀਦ ਹੈ ਕਿ ਇਹ ਜਾਣਕਾਰੀ ਤੁਹਾਡੇ ਲਈ ਲਾਭਦਾਇਕ ਰਹੀ। ਤੁਸੀਂ ਮੌਸਮ, ਫ਼ਸਲ ਦੀ ਸਲਾਹ ਜਾਂ ਸਕੀਮਾਂ ਲਈ ਕਿਸੇ ਵੀ ਵੇਲੇ ਇਸ ਹੈਲਪਲਾਈਨ ਉੱਤੇ ਫ਼ੋਨ ਕਰ ਸਕਦੇ ਹੋ। ਤੁਹਾਨੂੰ ਚੰਗੀ ਫ਼ਸਲ ਅਤੇ ਸਫ਼ਲ ਸੀਜ਼ਨ ਦੀ ਸ਼ੁਭਕਾਮਨਾ।

### Tamil (`ta`)

> வேளாண்மை மற்றும் விவசாயிகள் நலத்துறை அமைச்சகத்தின் சேவையான பாரத் விஸ்தார் உதவி எண்ணை அழைத்தமைக்கு நன்றி. இந்தத் தகவல் உங்களுக்குப் பயனுள்ளதாக இருந்திருக்கும் என நம்புகிறேன். வானிலை, பயிர் ஆலோசனை அல்லது திட்டங்களுக்காக இந்த உதவி எண்ணை நீங்கள் எப்போது வேண்டுமானாலும் அழைக்கலாம். நல்ல விளைச்சலும் வெற்றிகரமான பருவமும் அமைய வாழ்த்துக்கள்.

### Telugu (`te`)

> వ్యవసాయ మరియు రైతు సంక్షేమ మంత్రిత్వ శాఖ సేవ అయిన భారత్ విస్తార్ హెల్ప్‌లైన్‌కు ఫోన్ చేసినందుకు ధన్యవాదాలు. ఈ సమాచారం మీకు ఉపయోగపడిందని ఆశిస్తున్నాను. వాతావరణం, పంట సలహా లేదా పథకాల కోసం మీరు ఎప్పుడైనా ఈ హెల్ప్‌లైన్‌కు ఫోన్ చేయవచ్చు. మంచి పంట మరియు విజయవంతమైన సీజన్ కోరుకుంటున్నాను.

### Kannada (`kn`)

> ಕೃಷಿ ಮತ್ತು ರೈತರ ಕಲ್ಯಾಣ ಸಚಿವಾಲಯದ ಸೇವೆಯಾದ ಭಾರತ್ ವಿಸ್ತಾರ್ ಸಹಾಯವಾಣಿಗೆ ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ಮಾಹಿತಿ ನಿಮಗೆ ಉಪಯುಕ್ತವಾಗಿದೆ ಎಂದು ಭಾವಿಸುತ್ತೇನೆ. ಹವಾಮಾನ, ಬೆಳೆ ಸಲಹೆ ಅಥವಾ ಯೋಜನೆಗಳಿಗಾಗಿ ನೀವು ಯಾವಾಗ ಬೇಕಾದರೂ ಈ ಸಹಾಯವಾಣಿಗೆ ಕರೆ ಮಾಡಬಹುದು. ನಿಮಗೆ ಉತ್ತಮ ಬೆಳೆ ಮತ್ತು ಯಶಸ್ವಿ ಹಂಗಾಮು ಸಿಗಲಿ.

### Malayalam (`ml`)

> കൃഷി-കർഷകക്ഷേമ മന്ത്രാലയത്തിന്റെ സേവനമായ ഭാരത് വിസ്താർ ഹെൽപ്പ്‌ലൈനിലേക്ക് വിളിച്ചതിന് നന്ദി. ഈ വിവരങ്ങൾ താങ്കൾക്ക് ഉപകാരപ്രദമായിരുന്നു എന്ന് ഞാൻ പ്രതീക്ഷിക്കുന്നു. കാലാവസ്ഥ, വിള ഉപദേശം അല്ലെങ്കിൽ പദ്ധതികൾ എന്നിവയ്ക്കായി താങ്കൾക്ക് എപ്പോൾ വേണമെങ്കിലും ഈ ഹെൽപ്പ്‌ലൈനിലേക്ക് വിളിക്കാം. നല്ല വിളവും വിജയകരമായ സീസണും നേരുന്നു.

### Gujarati (`gu`)

> ભારત વિસ્તાર હેલ્પલાઇન, કૃષિ અને ખેડૂત કલ્યાણ મંત્રાલયની સેવા, પર ફોન કરવા બદલ આપનો આભાર. મને આશા છે કે માહિતી આપને ઉપયોગી રહી. આપ હવામાન, પાક સલાહ કે યોજનાઓ માટે આ હેલ્પલાઇન પર ક્યારેય પણ ફોન કરી શકો છો. આપને સારા પાક અને સફળ ઋતુની શુભકામનાઓ.

### Marathi (`mr`)

> कृषी आणि शेतकरी कल्याण मंत्रालयाची सेवा असलेल्या भारत विस्तार हेल्पलाइनला फोन केल्याबद्दल धन्यवाद. ही माहिती आपल्याला उपयुक्त ठरली असेल अशी आशा आहे. हवामान, पीक सल्ला किंवा योजनांसाठी आपण या हेल्पलाइनवर कधीही फोन करू शकता. आपल्याला चांगले पीक आणि यशस्वी हंगामासाठी शुभेच्छा.

### Bengali (`bn`)

> কৃষি ও কৃষক কল্যাণ মন্ত্রণালয়ের পরিষেবা ভারত বিস্তার হেল্পলাইনে ফোন করার জন্য ধন্যবাদ। আশা করি তথ্যগুলি আপনার কাজে লেগেছে। আবহাওয়া, ফসলের পরামর্শ বা প্রকল্পের জন্য আপনি যেকোনো সময় এই হেল্পলাইনে ফোন করতে পারেন। আপনার ফসল ভালো হোক এবং মরশুম সফল হোক।

## Sarvam repeat/error messages

These messages come from the supplied Sarvam agent configuration, not from
`bh-voice-prod`. They are the closest configured equivalent to an “Are you
there?” or silence-recovery message. Sarvam uses Hindi as the default language.

| Language | Code | Message |
|---|---:|---|
| English | `en` | Can you please say your question once again. |
| Hindi | `hi` | कृपया अपना सवाल एक बार फिर बोल दीजिए। |
| Bengali | `bn` | অনুগ্রহ করে আপনার প্রশ্নটি আবার বলুন। |
| Gujarati | `gu` | કૃપા કરીને તમારો પ્રશ્ન ફરીથી કહો। |
| Kannada | `kn` | ದಯವಿಟ್ಟು ನಿಮ್ಮ ಪ್ರಶ್ನೆಯನ್ನು ಮತ್ತೆ ಹೇಳಿ। |
| Malayalam | `ml` | ദയവായി നിങ്ങളുടെ ചോദ്യം വീണ്ടും പറയൂ. |
| Marathi | `mr` | कृपया करून तुमचा प्रश्न पुन्हा सांगा. |
| Odia | `od` | ଦୟାକରି ଆପଣଙ୍କର ପ୍ରଶ୍ନ ପୁଣିଥରେ କୁହନ୍ତୁ। |
| Punjabi | `pa` | ਕਿਰਪਾ ਕਰਕੇ ਆਪਣਾ ਸਵਾਲ ਮੁੜ ਦੱਸੋ। |
| Tamil | `ta` | தயவுசெய்து உங்கள் கேள்வியை மீண்டும் கூறுங்கள். |
| Telugu | `te` | దయచేసి మీ ప్రశ్నను మళ్లీ చెప్పండి. |

There is no literal “Are you there?” message in the production backend. The
backend contains a `NUDGE_API_URL` setting, but this branch does not invoke it.
