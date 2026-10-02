from types import SimpleNamespace
from unittest.mock import Mock
import shutil

import numpy as np
import pytest

from src.dexa_ocr.services.ocr_engine import PaddleOCREngine, TesseractEngine
from src.dexa_ocr.services.table_reader import find_table_boxes, read_positioned_table
from tests.test_table_geometry import fixture


def test_paddle_v2_retains_boxes_and_low_confidence_cells():
    engine=PaddleOCREngine.__new__(PaddleOCREngine)
    engine._ocr=Mock()
    engine._ocr.ocr.return_value=[[
        [[[10,20],[30,20],[30,30],[10,30]],("-1.2",.30)],
        [[[40,20],[60,20],[60,30],[40,30]],("-0.1",.99)],
    ]]
    tokens=engine.recognize_tokens(np.zeros((60,80),dtype=np.uint8))
    assert tokens[0].text=="-1.2" and tokens[0].confidence==.30
    assert tokens[1].left==40
    assert engine.recognize(np.zeros((60,80),dtype=np.uint8))=="-0.1"


def test_paddle_v3_retains_box_and_confidence():
    engine=PaddleOCREngine.__new__(PaddleOCREngine)
    engine._ocr=Mock()
    engine._ocr.ocr.return_value=[dict(rec_texts=["-1.2"],rec_scores=[.95],
        dt_polys=[[[10,20],[30,20],[30,30],[10,30]]])]
    token=engine.recognize_tokens(np.zeros((60,80),dtype=np.uint8))[0]
    assert token.text=="-1.2" and token.confidence==.95 and token.bottom==30


def test_tesseract_tokens_keep_missing_cell_geometry():
    engine=TesseractEngine.__new__(TesseractEngine)
    engine._default_lang="eng"
    engine._pytesseract=Mock()
    engine._pytesseract.image_to_data.return_value=dict(
        text=["", "-1.2", "-0.1"],left=[0,100,200],top=[0,20,20],
        width=[0,30,30],height=[0,10,10],conf=[-1,30,99])
    tokens=engine.recognize_tokens(np.zeros((80,300),dtype=np.uint8))
    assert len(tokens)==2 and tokens[0].confidence==.3 and tokens[1].left==200


def test_rectangle_above_old_crop_is_located_and_read():
    import cv2
    image=np.full((910,643,3),255,dtype=np.uint8)
    cv2.rectangle(image,(299,359),(623,457),(0,0,0),1)
    boxes=find_table_boxes(image)
    assert len(boxes)==1 and boxes[0][1]<.44*image.shape[0]
    engine=Mock()
    engine.recognize_tokens.return_value=fixture()
    result=read_positioned_table(image,engine)
    assert "-1.2" in result.text and "-0.9" in result.text
    assert engine.recognize_tokens.call_args.kwargs["psm"]==6


def test_borderless_page_uses_positioned_full_page_read():
    engine=Mock()
    engine.recognize_tokens.return_value=fixture(False)
    result=read_positioned_table(np.full((910,643,3),255,dtype=np.uint8),engine)
    assert "-1.5" in result.text
    assert engine.recognize_tokens.call_args.kwargs["psm"]==11


def test_pipeline_uses_canonical_columns_even_for_hologic(monkeypatch,tmp_path):
    import src.dexa_ocr.pipeline as pipeline
    from src.dexa_ocr.services.table_geometry import TableExtraction
    monkeypatch.setattr(pipeline,"detect_manufacturer",lambda img:"hologic")
    monkeypatch.setattr(pipeline,"extract_rois",lambda img,rois:{})
    monkeypatch.setattr(pipeline,"read_positioned_table",lambda *a,**k:TableExtraction(
        "Colo 0.872 - -1.2 - -0.1",["cell review"]))
    settings=SimpleNamespace(roi_strategy="fixed",tesseract_lang="eng")
    texts=pipeline.process_report_page(np.zeros((100,100,3),dtype=np.uint8),
        "page",Mock(),settings,tmp_path,False)
    assert texts["_manufacturer"]=="hologic"
    assert texts["_table_format"]=="ge"
    assert texts["_table_warnings"]=="cell review"
    assert texts["table_data"]=="Colo 0.872 - -1.2 - -0.1"


def test_engine_without_boxes_does_not_fall_back_to_unsafe_text(monkeypatch,tmp_path):
    import src.dexa_ocr.pipeline as pipeline
    monkeypatch.setattr(pipeline,"detect_manufacturer",lambda img:"ge")
    monkeypatch.setattr(pipeline,"extract_rois",lambda img,rois:{})
    def unavailable(*args,**kwargs):
        raise NotImplementedError
    monkeypatch.setattr(pipeline,"read_positioned_table",unavailable)
    texts=pipeline.process_report_page(np.zeros((100,100,3),dtype=np.uint8),
        "page",Mock(),SimpleNamespace(roi_strategy="fixed",tesseract_lang="eng"),tmp_path,False)
    assert texts["table_data"]=="" and texts["_table_warnings"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract binary not installed")
@pytest.mark.parametrize("compact",[True,False])
def test_real_tesseract_on_anonymous_rendered_tables(compact):
    import cv2
    from src.dexa_ocr.services.parser_table import parse_measurement_table
    image=np.full((1000,1800,3),255,dtype=np.uint8)
    cv2.rectangle(image,(100,300),(1650,650),(0,0,0),2)
    def label(text,x,y):
        size=cv2.getTextSize(text,cv2.FONT_HERSHEY_SIMPLEX,1,2)[0]
        cv2.putText(image,text,(int(x-size[0]/2),y),cv2.FONT_HERSHEY_SIMPLEX,1,(0,0,0),2)
    label("BMD",600,370);label("T-score",1000,400);label("Z-score",1400,400)
    if not compact:
        label("(%)",800,400);label("(%)",1200,400)
    for y,name,bmd,t,z in [(470,"Colo","0.872","-1.2","-0.1"),(540,"Total","0.898","-0.9","-0.1")]:
        for text,x in [(name,250),(bmd,600),(t,1000),(z,1400)]:
            label(text,x,y)
        if not compact:
            label("80",800,y);label("98",1200,y)
    engine=TesseractEngine(SimpleNamespace(tesseract_cmd=None,tesseract_lang="eng"))
    result=read_positioned_table(image,engine,"eng")
    parsed=parse_measurement_table(result.text,"right_femur")
    assert [r.t_score for r in parsed]==[-1.2,-.9]
    assert [r.z_score for r in parsed]==[-.1,-.1]
