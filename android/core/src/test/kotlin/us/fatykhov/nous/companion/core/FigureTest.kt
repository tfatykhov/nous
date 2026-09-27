package us.fatykhov.nous.companion.core

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** Every assertion from the web's figure.test.ts — each pins a regex behaviour Java could differ on. */
class FigureTest {
    private fun fig(vararg v: String) = v.forEach { assertTrue(Figure.isFigureValue(it), "expected figure: '$it'") }
    private fun prose(vararg v: String) = v.forEach { assertFalse(Figure.isFigureValue(it), "expected prose: '$it'") }

    @Test fun prose() {
        prose("payment pending", "deposit paid · EUR811.44 balance at check-in", "approved 3 of 5 requests", "down 4% from last week")
    }

    @Test fun figures() {
        fig("EUR 1,234,567.89", "-12.5%", "2026-09-02T15:40:43Z", "2026-09-02T15:40:43+00:00", "2026-09-02T15:40:43-04:00", "2026-09-02T15:40:43.250+0200")
        prose("2026-09-02T15:40:43+")
        fig("0.8 /day", "3/wk", "12 km/h", "EUR 5 /mo", "↑0.8 /day"); prose("0.8 / day of rest")
        fig("16 °C", "98.6°F", "−2 °C"); prose("16 °Celsius")
        fig("-$1,234.56", "−$3", "$-5", "-EUR 5", "EUR -5"); prose("-$-5")
        fig("42 €", "1.234,56 €", "1.234,56 €", "-42 €", "€ 42"); prose("42 € extra")
        fig("1 234,56 €", "1 234,56 €", "1'234.56", "1 234", "1 234 people")
        fig("١٬٢٣٤٫٥٦", "۱۲۳", "↑١٢ km", "١٢ كيلومترات")
        fig("<5%", "≥95 bpm", "≤1.2 ms", "> $1,000", "~42", "±0.3 kg"); prose("< five")
        fig("R$1,234.56", "CA$1,234.56", "US$ 1.234,56", "-R$5", "42 US\$"); prose("ABCD$5")
        fig("1e6", "1.2e-6", "1E+09", "3e8 m/s"); prose("1e", "1e6e", "1 E", "e6", "1e+")
        fig("1 234,56 \$US", "\$US 5", "؜-١٬٢٣٤٫٥٦", "‏١٬٢٣٤٫٥٦ US\$"); prose("‏مرحبا")
        fig("%12", "١٢٪", "12 ‰", "($1,234.56)", "(US$1,234.56)", "(1 234,56 \$US)", "(EUR 5)", "12 км", "1,2 Mio.", "1.2万", "12 километров"); prose("(12", "12)")
        fig("-%12", "+%12", "∞", "-∞", "$∞", "∞%", "↑∞"); prose("-%-12", "infinity")
        fig("9/4/2026", "04/09/2026", "04.09.2026", "2026/09/04 15:40", "9/4/2026, 3:40 PM"); prose("9/4/2026/1", "9/4/2026/12")
        fig("Sep 4, 2026", "4 Sept 2026", "4. Sept. 2026", "4 sept. 2026", "September 4, 2026, 3:40 PM", "NaN", "\$NaN", "NaN%"); prose("Sep 4 2026 and more", "not a number")
        fig("3–5", "3-5", "$3 – $5", "10%–20%", "EUR 3–EUR 5", "-1 234,56 ₽", "-1,234.56 ₪", "-1.235 ₫", "¢99"); prose("3–", "3–5–7")
        fig("۱۴:۳۰", "২:৩০ PM", "٢:٣٠ م", "2:30 p.m.", "٤/٩/٢٠٢٦ ٢:٣٠ م", "١٢/٣٠"); prose("2:30 later")
        fig("12.3 kilometers", "12.3 kilometers per hour", "12,3 Kilometer pro Stunde", "12,3 kilómetros por hora", "5 minutes")
        prose("42 requests still pending", "5 days ago", "3 items in the basket", "12 apples, 3 pears")
        fig("時速 12.3 キロメートル", "약 12 km", "Total 42"); prose("speed 12.3 kilometers per hour")
        fig("12.3 मेगाबाइट", "4 सितंबर 2026", "12 กิโลเมตร"); prose("২:৩০ পূর্বাহ্ণ")
        fig("↓$5", "↑€1,200", "↓EUR 5", "↑%12", "▲ 3–5"); prose("↑ up and away")
        fig("10:00–11:00", "9/4/2026 – 9/6/2026", "Sep 4, 2026 – Sep 6, 2026", "2026-09-04 – 2026-09-06", "1234,56 km/godz.", "12 Std./Tag"); prose("10:00–")
        fig("4 – 6 Sept 2026", "Sep 4 – 6, 2026", "04.–06.09.2026", "9/4 – 9/6/2026"); prose("4 – 6 Sept 2026 and later")
        fig("١٢٫٣°م", "12.3 °C"); prose("12°Celsius")
        fig("PM 2:30", "下午2:30", "৪ সেপ, ২০২৬", "4 ก.ย. 2569", "4 सित॰ 2026"); prose("PM 2:30 sharp")
        fig("—", "42", "3.14", "-7", "+1.5", "65.0 bpm", "3 kg", "5 km", "12 h", "98.6°", "$1,234.56", "€42", "£100", "12/30", "3/4", "2026-09-02", "14:30", "2:30 PM", "08:05:00", "45s", "1h 30m", "2d", "-", "–", "N/A", "n/a", "", "   ")
        fig("↓0.03", "↑6 %", "↑0.02", "↑0.8 /day", "↑3", "↑1.5", "↓1.5", "▲1.5", "▼1.5", "−1.5", "↓1.2 /week", "▲3 /month")
        prose("↑ improving trend over last 30 days", "↑6 and ↓3")
    }

    @Test fun tightUnit() {
        for (u in listOf("%", "％", "٪", "‰", "°C", "′")) assertTrue(Figure.isTightUnit(u), u)
        for (u in listOf("kg", "bpm", "€", "")) assertFalse(Figure.isTightUnit(u), u)
    }
}
