"""Anonymous synthetic fixtures; no patient images/names in the repository."""
import pytest

from src.dexa_ocr.services.table_geometry import OCRToken, extract_table
from src.dexa_ocr.services.parser_table import build_site_result, parse_measurement_table


def tok(text, x, y, confidence=1.0, width=30):
    return OCRToken(text, x-width/2, y-5, x+width/2, y+5, confidence)


def fixture(compact=True, scale=1, offset=0):
    headers = [tok("BMD", 150, 90), tok("T-score", 300, 100), tok("Z-score", 450, 100)]
    if not compact:
        headers += [tok("(%)", 240, 100), tok("(%)", 390, 100)]
    values = [("Colo", ".872", "-1.2", "-.1"), ("Total", ".898", "-.9", "-.1")]
    if not compact:
        values = [("Colo", ".826", "-1.5", "-.1"), ("Total", ".801", "-1.6", "-.4")]
    tokens = headers[:]
    for y,(name,bmd,t,z) in zip((125,145),values):
        # Real reports include a leading zero, retained here.
        bmd = "0"+bmd
        t = t.replace("-.", "-0.")
        z = z.replace("-.", "-0.")
        tokens += [tok(name,50,y),tok(bmd,150,y),tok(t,300,y),tok(z,450,y)]
        if not compact:
            tokens += [tok("80",240,y),tok("98" if y==125 else "94",390,y)]
    return [OCRToken(t.text, t.left*scale+offset, t.top*scale+offset,
                     t.right*scale+offset,t.bottom*scale+offset,t.confidence) for t in tokens]


def rows(tokens):
    result = extract_table(tokens)
    return result, parse_measurement_table(result.text,"right_femur")


@pytest.mark.parametrize("compact,expected", [(True,[-1.2,-.9]),(False,[-1.5,-1.6])])
@pytest.mark.parametrize("scale,offset", [(1,0),(3,57),(.75,120)])
def test_layouts_are_position_independent(compact,expected,scale,offset):
    result, parsed = rows(fixture(compact,scale,offset))
    assert not result.warnings
    assert [r.t_score for r in parsed] == expected
    assert [r.z_score for r in parsed] == ([-.1,-.1] if compact else [-.1,-.4])
    assert parsed[0].young_adult_percent == (None if compact else 80)


@pytest.mark.parametrize("compact", [True,False])
def test_missing_t_does_not_consume_z_or_percentage(compact):
    tokens = [t for t in fixture(compact) if not (t.x==300 and t.y==125)]
    result, parsed = rows(tokens)
    assert parsed[0].t_score is None
    assert parsed[0].z_score == -.1
    assert result.warnings


def test_low_confidence_cell_keeps_its_position():
    tokens = fixture()
    tokens = [tok(t.text,t.x,t.y,.1) if t.x==300 and t.y==125 else t for t in tokens]
    result, parsed = rows(tokens)
    assert parsed[0].t_score is None and parsed[0].z_score == -.1
    assert result.warnings


def test_swapped_score_columns_and_positive_values():
    tokens = fixture()
    tokens = [tok("+2.1" if t.text=="-1.2" else t.text,
                  450 if t.x==300 else 300 if t.x==450 else t.x,t.y) for t in tokens]
    result, parsed = rows(tokens)
    assert not result.warnings
    assert parsed[0].t_score == 2.1 and parsed[0].z_score == -.1


def test_header_is_required_even_if_numbers_look_plausible():
    result = extract_table([t for t in fixture() if t.text!="T-score"])
    assert not result.text and result.warnings


def test_two_tables_are_ambiguous():
    result = extract_table(fixture()+fixture(offset=600))
    assert not result.text and result.warnings


def test_trend_rows_cannot_replace_current_measurement():
    tokens = fixture()+[tok("Tendência",50,170),tok("Total",50,190),
                        tok("0.500",150,190),tok("-3.0",300,190),tok("-2.0",450,190)]
    result, parsed = rows(tokens)
    assert len(parsed)==2 and parsed[-1].t_score==-.9


def test_hologic_headers_and_extra_area_cmo_columns():
    tokens = fixture()
    tokens = [tok("DMO" if t.text=="BMD" else "Escore T" if t.text=="T-score"
                  else "Escore Z" if t.text=="Z-score" else t.text,t.x,t.y) for t in tokens]
    for y in (125,145):
        tokens += [tok("10.2",90,y,width=15),tok("8.0",115,y,width=15)]
    result, parsed = rows(tokens)
    assert [r.t_score for r in parsed]==[-1.2,-.9]


def test_missing_cells_and_split_minus_do_not_shift():
    tokens = [t for t in fixture() if not (t.x==300 and t.y==125)]
    tokens += [tok("-",291,125,width=4),tok("1.2",304,125,width=18)]
    result, parsed = rows(tokens)
    assert parsed[0].t_score == -1.2


def test_hologic_pr_am_percentages_are_not_interchanged():
    tokens = fixture()+[tok("PR (%)",350,100),tok("AM (%)",500,100)]
    for y in (125,145):
        tokens += [tok("80",350,y),tok("98",500,y)]
    result, parsed = rows(tokens)
    assert parsed[0].young_adult_percent == 80
    assert parsed[0].age_matched_percent == 98


def test_missing_bmd_does_not_consume_adjacent_cmo():
    tokens = [t for t in fixture() if not (t.x==150 and t.y==125)]
    tokens += [tok("CMO",110,90),tok("0.555",110,125,width=20)]
    result, parsed = rows(tokens)
    assert parsed[0].bmd is None and parsed[0].t_score == -1.2


def test_trochanter_period_and_blank_diaphysis():
    tokens = fixture()+[tok("Troc.",50,165),tok("0.731",150,165),
        tok("-1.0",300,165),tok("-0.4",450,165),tok("Diafise",50,185),
        tok("1.075",150,185),tok("-",300,185),tok("-",450,185)]
    result, parsed = rows(tokens)
    assert parsed[2].region == "Troc." and parsed[2].t_score == -1.0
    assert parsed[3].bmd == 1.075 and parsed[3].t_score is None


def test_compact_text_requires_header_and_exact_cells():
    data="Colo 0,872 -1,2 -0,1\nTotal 0,898 -0,9 -0,1"
    site=build_site_result("Fêmur direito","Região BMD T-score Z-score",data,"ge")
    assert [r.t_score for r in site.rows]==[-1.2,-.9]
    # Unlabelled compact data is not assumed to be a five-column GE row.
    parsed=parse_measurement_table(data,"right_femur")
    assert all(r.t_score is None and r.z_score is None for r in parsed)


@pytest.mark.parametrize("line", ["Colo 0.872 -1.2 -0.1", "Colo 0.872 80 -1.2 98 -0.1 7"])
def test_incomplete_or_extra_text_cells_never_shift_scores(line):
    parsed=parse_measurement_table(line,"right_femur")
    assert parsed[0].bmd==.872
    assert parsed[0].t_score is None and parsed[0].z_score is None
