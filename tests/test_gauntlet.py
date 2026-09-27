"""The adversarial gauntlet as a test: any failure in any section fails the build."""
import os

os.environ["VERA_DB_PATH"] = ""
os.environ["LLM_ENABLED"] = "0"
os.environ["VERA_SESSION_IDLE_RESET"] = "0"


def test_gauntlet_clean():
    from eval import gauntlet as g
    g.FAIL.clear(); g.STATS.clear()
    C = g.Client(None)
    cats, ms, cs, ts = g.load()
    g.section_a(C); g.section_b(C, cats, ms, cs, ts); g.section_c(C, cats, ms, cs, ts)
    g.section_d(C, cats, ms, cs, ts); g.section_e(C, cats, ms, ts); g.section_f(C, cats, ms); g.section_g(cats, ms, ts)
    C.reset()
    assert not g.FAIL, {k: v[:3] for k, v in g.FAIL.items()}
    assert sum(g.STATS.values()) > 1500
