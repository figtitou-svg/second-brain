from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import repeat_guard as rg

T0 = datetime(2026, 11, 18, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))


def test_third_identical_message_is_blocked_and_reported_once():
    sent, clock = [], [T0]
    g = rg.RepeatGuard(lambda text, buttons=None: sent.append(text), now_fn=lambda: clock[0])
    blocks, clears = [], []
    g.on_block, g.on_clear = (lambda k, n: blocks.append(n)), (lambda: clears.append(1))
    msg = "🔀 « Campus du Libre » passe avant : <b>[Esp] Ejervicio</b> déplacée"
    for i in range(6):
        clock[0] = T0 + timedelta(minutes=5 * i)
        g(msg)
        g(f"⏰ autre message {i}")                          # les autres messages passent toujours
    assert sent.count(msg) == 2
    alerts = [s for s in sent if "Message répété bloqué" in s]
    assert len(alerts) == 1 and "&lt;" not in alerts[0] and "Campus du Libre" in alerts[0]
    assert blocks == [3]
    clock[0] = T0 + timedelta(hours=5)                       # la boucle s'est arrêtée depuis longtemps
    g(msg)
    assert sent.count(msg) == 3 and clears == [1]
