"""
=======================================================================
  하천 불법점용 AI 단속 시스템 v5.0
  베이스: v4.2 (안정 버전)
  추가  : 암막(블라인드) 처리
          - 구역당 원본 1장 + 암막 1장 캡처
          - API 호출은 구역당 1회 (2장을 한 번에 전송)
          - mask_layer: dataProvider 직접 조작 (편집 세션 없음)
=======================================================================
"""

import os
import gc
import time
import google.generativeai as genai
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from qgis.core import (QgsProject, QgsVectorLayer, QgsFeature, QgsField,
                       QgsFillSymbol, QgsGeometry)
from qgis.PyQt.QtCore import QVariant, QCoreApplication
from qgis.utils import iface

# =====================================================================
# 🧹 이전 실행 변수 정리
# =====================================================================
_cleanup_vars = ["poly_layer", "grid_layer", "canvas", "model",
                 "capture_records", "results", "features",
                 "mask_layer", "mask_provider", "fewshot_bytes_list"]

for _v in _cleanup_vars:
    if _v in globals():
        try:
            del globals()[_v]
        except Exception:
            pass

gc.collect()
QCoreApplication.processEvents()

# =====================================================================
# ⚙️ 설정
# =====================================================================
API_KEY           = "여기에_API_키를_입력하세요"
MODEL_NAME        = "gemini-2.5-flash"

MAX_WORKERS       = 8
CANVAS_WAIT_SEC   = 0.4
RETRY_COUNT       = 3

PHOTO_FOLDER      = r"E:\하천점용AI\사진"
FEWSHOT_FOLDER    = r"E:\하천점용AI\사진\오탐예시"
EXTENT_SCALE      = 1.5
OUTPUT_LAYER_NAME = "AI_단속_의심구역"
MASK_LAYER_NAME   = "AI_시야차단_마스크"

# =====================================================================
# 📂 Few-shot 예시 정의
# =====================================================================
FEWSHOT_EXAMPLES = [
    {"filename": "1경작지_불법주.png",      "answer": "정상",       "reason": "하천 내 자연식생(갈대·풀)은 경작지 아님. 경계 내부에 경작지·주차 없음."},
    {"filename": "2비닐하우스_경작지.png",  "answer": "정상",       "reason": "비닐하우스는 경계 외부. 내부 구조물은 하천 보(洑)로 불법점용 아님."},
    {"filename": "7경작지_자재적치.png",    "answer": "정상",       "reason": "자재·건물이 경계 외부. 경계 내부는 자연 하천 식생."},
    {"filename": "zone_0.png",              "answer": "정상",       "reason": "하천 내 자연식생은 경작지 아님. 경계 내부에 경작지 없음."},
    {"filename": "zone_9.png",              "answer": "가설건축물", "reason": "건물이 경계 내부에 있음."},
    {"filename": "10함정_하천바닥자갈.png", "answer": "정상",       "reason": "자연적인 강바닥 자갈 무더기. 불법 시설 아님."},
    {"filename": "11함정_하천바닥자갈.png", "answer": "정상",       "reason": "인위적 적재 흔적 없는 자연 건천 바닥."},
    {"filename": "12함정_하천바닥자갈.png", "answer": "정상",       "reason": "자연석·자갈 모래톱. 자재적치 오탐 제외."},
    {"filename": "zone_18_masked.png", "answer": "정상",       "reason": "인위적 적재 흔적 없는 자연 건천 바닥."},
    {"filename": "zone_27_masked.png", "answer": "정상",       "reason": "하천 내 자연식생은 경작지 아님. 자연석·자갈 모래톱. 자재적치 오탐 제외."},
]

# =====================================================================
# 📋 카테고리 정의
# =====================================================================
CATEGORIES = [
    ("비닐하우스", ["비닐하우스", "비닐"]),
    ("가설건축물", ["가설건축물", "가설", "컨테이너", "패널"]),
    ("경작지",     ["경작지", "경작", "밭", "작물"]),
    ("자재적치",   ["자재적치", "자재", "골재", "폐기물", "야적"]),
]
FIELD_NAMES = [cat[0] for cat in CATEGORIES]

# =====================================================================
# 📋 프롬프트 — 2장 구조에 맞게 수정
# =====================================================================
PROMPT_MAIN = """
당신은 하천 불법점용 단속 전문가입니다.
아래에 같은 구역의 사진이 2장 제공됩니다.

[사진 구성]
- 1번 사진 (전체 맥락): 주변 지형과 함께 찍은 원본. 노란 경계선이 표시되어 있음
- 2번 사진 (암막 강조): 경계선 외부가 검정으로 가려진 사진. 내부만 밝게 표시됨

[판독 방법]
- 1번 사진으로 주변 맥락(도로·농경지 등) 파악
- 2번 사진의 밝은 영역(경계 내부)에서 불법점용 여부 최종 판단
- 검정(암막) 영역은 100% 무시

[판독 대상 — 경계 내부에서 명백히 확인될 때만]
- 비닐하우스: 흰색/반투명 아치형 구조물. 연속 아치 패턴 필요
- 가설건축물: 컨테이너, 패널 조립 건물 (하천 보·수문·교량 제외)
- 경작지: 인위적으로 조성된 규칙적 이랑(두둑) 패턴이 명확히 보여야 함
- 자재적치: 골재 더미, 폐기물, 건자재 야적

[절대 포함하지 말 것]
- 암막(검정) 영역에 있는 모든 시설물
- 자연식생(갈대·억새·풀): 이랑 패턴 없으면 경작지 아님
- 하천 보·수문·제방: 정상 하천 시설물
- 건천 모래·자갈 바닥: 경작지·자재적치 아님

[출력 규칙]
1. 경계 내부에 명백한 불법점용: 항목명만 쉼표로 나열 (예: 비닐하우스, 경작지)
2. 복수 항목이면 모두 나열
3. 불확실하거나 자연 지형뿐이면: 정상
4. 설명·이유·문장 절대 금지 — 단어/쉼표만
"""

# =====================================================================
# ✅ 유틸 함수
# =====================================================================
def img_to_bytes(path):
    try:
        buf = BytesIO()
        Image.open(path).convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None

def bytes_to_pil(data):
    return Image.open(BytesIO(data))

def extract_text(response):
    if not response.candidates:
        return ""
    parts = response.candidates[0].content.parts
    text_parts = [p.text for p in parts if hasattr(p, "text") and p.text]
    return text_parts[-1].strip() if text_parts else ""

def parse_detections(result_text):
    flags    = {name: 0 for name in FIELD_NAMES}
    detected = []
    if not result_text or "정상" in result_text:
        return flags, detected
    for field_name, keywords in CATEGORIES:
        for kw in keywords:
            if kw in result_text:
                flags[field_name] = 1
                detected.append(field_name)
                break
    return flags, detected

def safe_remove_layers(layer_name):
    """레이어 안전 삭제 — ID 수집 후 참조 해제 후 삭제"""
    existing = QgsProject.instance().mapLayersByName(layer_name)
    if not existing:
        return
    ids = [lyr.id() for lyr in existing]
    del existing
    for lid in ids:
        QgsProject.instance().removeMapLayer(lid)
    QCoreApplication.processEvents()

# =====================================================================
# 🔧 초기화
# =====================================================================
print("=" * 65)
print("  하천 불법점용 AI 단속 시스템 v5.0  (암막 처리 추가)")
print("=" * 65)

genai.configure(api_key=API_KEY)
model      = genai.GenerativeModel(MODEL_NAME)
GEN_CONFIG = genai.types.GenerationConfig(temperature=0.1, max_output_tokens=200)

# Few-shot 이미지 사전 로드 (시작 시 1회만)
os.makedirs(FEWSHOT_FOLDER, exist_ok=True)
fewshot_bytes_list = []
for ex in FEWSHOT_EXAMPLES:
    p = os.path.join(FEWSHOT_FOLDER, ex["filename"])
    b = img_to_bytes(p)
    if b is not None:
        desc = (f"[판독 예시] 아래 사진의 정답: {ex['answer']}\n"
                f"이유: {ex['reason']}")
        fewshot_bytes_list.append((desc, b))

print(f"🤖 AI 모델       : {MODEL_NAME}")
print(f"⚡ 병렬 워커     : {MAX_WORKERS}개")
print(f"🖼️  Few-shot 예시 : {len(fewshot_bytes_list)}개 로드됨")

# =====================================================================
# 🗺️ QGIS 레이어 준비
# =====================================================================
grid_layer = iface.activeLayer()
if grid_layer is None:
    raise RuntimeError("❌ 레이어 창에서 하천 폴리곤 레이어를 선택하세요!")

print(f"\n📌 분석 대상 레이어: [{grid_layer.name()}]")

# 기존 결과/마스크 레이어 안전 삭제
safe_remove_layers(OUTPUT_LAYER_NAME)
safe_remove_layers(MASK_LAYER_NAME)
iface.mapCanvas().refresh()
QCoreApplication.processEvents()

# 결과 레이어 생성
poly_layer = QgsVectorLayer(
    f"Polygon?crs={grid_layer.crs().authid()}",
    OUTPUT_LAYER_NAME, "memory"
)
attrs = [
    QgsField("detection", QVariant.String, len=200),
    QgsField("count",     QVariant.Int),
    QgsField("zone_id",   QVariant.Int),
]
for name in FIELD_NAMES:
    attrs.append(QgsField(name, QVariant.Int))
poly_layer.dataProvider().addAttributes(attrs)
poly_layer.updateFields()
QgsProject.instance().addMapLayer(poly_layer)

# ✅ 암막 레이어 생성 — dataProvider 방식 (편집 세션 없음)
mask_layer = QgsVectorLayer(
    f"Polygon?crs={grid_layer.crs().authid()}",
    MASK_LAYER_NAME, "memory"
)
symbol = QgsFillSymbol.createSimple({"color": "0,0,0,255", "outline_style": "no"})
mask_layer.renderer().setSymbol(symbol)
QgsProject.instance().addMapLayer(mask_layer)
mask_provider = mask_layer.dataProvider()   # 직접 참조 보관

os.makedirs(PHOTO_FOLDER, exist_ok=True)
features    = list(grid_layer.getFeatures())
total_count = len(features)
print(f"✅ 레이어 생성 완료")
print(f"\n📊 총 분석 구역: {total_count}개")

# =====================================================================
# 📸 [1단계] 캡처 — 구역당 원본 1장 + 암막 1장 (총 2장)
# =====================================================================
print(f"\n{'─'*55}")
print("📸 [1단계] 원본 + 암막 캡처 시작... (구역당 2장)")
print(f"{'─'*55}")

canvas          = iface.mapCanvas()
capture_records = []   # (feature, full_bytes, masked_bytes, count)
capture_start   = time.time()

for count, feature in enumerate(features, 1):
    geom   = feature.geometry()
    extent = geom.boundingBox()
    extent.scale(EXTENT_SCALE)

    canvas.setExtent(extent)
    canvas.refresh()

    deadline = time.time() + CANVAS_WAIT_SEC
    while time.time() < deadline or canvas.isDrawing():
        QCoreApplication.processEvents()
        if not canvas.isDrawing() and time.time() > (deadline - 0.05):
            break

    # ── 1장: 원본 캡처 (암막 없음) ──────────────────────────────
    full_path = os.path.join(PHOTO_FOLDER, f"zone_{feature.id()}_full.png")
    canvas.saveAsImage(full_path)
    full_bytes = img_to_bytes(full_path)

    # ── 암막 씌우기: dataProvider 직접 조작 (편집 세션 없음) ────
    bbox_geom  = QgsGeometry.fromRect(extent)
    donut_geom = bbox_geom.difference(geom)   # 도넛 = 전체 - 구역 내부
    mask_feat  = QgsFeature()
    mask_feat.setGeometry(donut_geom)

    mask_provider.truncate()                  # 기존 마스크 삭제
    mask_provider.addFeatures([mask_feat])    # 새 마스크 추가
    mask_layer.updateExtents()
    mask_layer.triggerRepaint()

    canvas.refresh()

    deadline = time.time() + CANVAS_WAIT_SEC
    while time.time() < deadline or canvas.isDrawing():
        QCoreApplication.processEvents()
        if not canvas.isDrawing() and time.time() > (deadline - 0.05):
            break

    # ── 2장: 암막 캡처 ──────────────────────────────────────────
    masked_path = os.path.join(PHOTO_FOLDER, f"zone_{feature.id()}_masked.png")
    canvas.saveAsImage(masked_path)
    masked_bytes = img_to_bytes(masked_path)

    # 암막 제거 (다음 구역 원본 캡처를 위해)
    mask_provider.truncate()
    mask_layer.triggerRepaint()

    capture_records.append((feature, full_bytes, masked_bytes, count))

    if count % 20 == 0 or count == total_count:
        elapsed = time.time() - capture_start
        print(f"  📷 {count:>5}/{total_count} 캡처 완료 ({elapsed:.1f}초)")

# 캡처 완료 후 마스크 레이어 제거
# ✅ Python 참조를 먼저 None으로 끊고 나서 C++ 객체 삭제 (2차 실행 크래시 방지)
mask_provider = None   # dataProvider 참조 먼저 해제
mask_layer    = None   # 레이어 참조 먼저 해제
gc.collect()           # 즉시 해제
safe_remove_layers(MASK_LAYER_NAME)
iface.mapCanvas().refresh()

capture_elapsed = time.time() - capture_start
print(f"\n✅ 캡처 완료: {total_count}개 / {capture_elapsed:.1f}초")

# =====================================================================
# 🤖 [2단계] AI 병렬 분석 — API 호출 1회에 2장 전송
# =====================================================================
def analyze_zone(record):
    """
    원본 + 암막 2장을 1번 API 호출에 전송
    API 호출 횟수는 구역당 1회로 동일
    """
    feature, full_bytes, masked_bytes, count = record

    if full_bytes is None or masked_bytes is None:
        return (feature, count, None, "이미지 변환 실패")

    for attempt in range(RETRY_COUNT + 1):
        try:
            # 콘텐츠 구성: 프롬프트 → few-shot → 원본 → 암막
            contents = [PROMPT_MAIN]

            for desc, fs_bytes in fewshot_bytes_list:
                contents.append(desc)
                contents.append(bytes_to_pil(fs_bytes))

            contents.append("── 1번 사진 (전체 맥락·원본) ──")
            contents.append(bytes_to_pil(full_bytes))       # 원본

            contents.append("── 2번 사진 (암막 강조·판독 기준) ──")
            contents.append(bytes_to_pil(masked_bytes))     # 암막

            response = model.generate_content(
                contents,
                generation_config=GEN_CONFIG
            )

            result = extract_text(response)
            result = result.replace("\n", " ").strip("[]「」【】").strip()
            if not result:
                result = "정상"

            return (feature, count, result, None)

        except Exception as e:
            if attempt < RETRY_COUNT:
                time.sleep(2 ** attempt)
            else:
                return (feature, count, None, str(e))


print(f"\n{'─'*55}")
print(f"🤖 [2단계] AI 병렬 판독 시작... (워커 {MAX_WORKERS}개)")
print(f"{'─'*55}")

ai_start        = time.time()
results         = []
error_count     = 0
normal_count    = 0
detect_count    = 0
category_counts = {name: 0 for name in FIELD_NAMES}

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    future_map = {
        executor.submit(analyze_zone, rec): rec
        for rec in capture_records
    }
    done = 0
    for future in as_completed(future_map):
        feature, count, status, error = future.result()
        done += 1

        if error:
            error_count += 1
            print(f"  ❌ [{done:>5}/{total_count}] 구역 {count:>4} → 실패: {error}")

        elif "정상" not in (status or "") and status:
            flags, detected = parse_detections(status)
            if detected:
                detect_count += 1
                for name in detected:
                    category_counts[name] += 1
                results.append((feature, status, flags, detected, count))
                marker = "🔴" if len(detected) >= 2 else "🎯"
                print(f"  {marker} [{done:>5}/{total_count}] 구역 {count:>4} → [{', '.join(detected)}] ({len(detected)}건)")
            else:
                detect_count += 1
                results.append((feature, status, {n: 0 for n in FIELD_NAMES}, [status], count))
                print(f"  🎯 [{done:>5}/{total_count}] 구역 {count:>4} → [{status}]")
        else:
            normal_count += 1
            print(f"  ✅ [{done:>5}/{total_count}] 구역 {count:>4} → 정상")

ai_elapsed = time.time() - ai_start

# =====================================================================
# 💾 [3단계] 결과 저장
# =====================================================================
print(f"\n💾 결과 저장 중...")
poly_layer.startEditing()
for feature, status, flags, detected, zone_id in results:
    f = QgsFeature()
    f.setGeometry(feature.geometry())
    attr_vals = [status, len(detected), zone_id]
    for name in FIELD_NAMES:
        attr_vals.append(flags.get(name, 0))
    f.setAttributes(attr_vals)
    poly_layer.addFeature(f)
poly_layer.commitChanges()
canvas.refresh()

# =====================================================================
# 📊 최종 리포트
# =====================================================================
total_elapsed = capture_elapsed + ai_elapsed
print(f"\n{'='*65}")
print(f"  🎉 임무 완료 — 최종 리포트 (v5.0)")
print(f"{'='*65}")
print(f"  📍 총 분석 구역  : {total_count:>6,}개")
print(f"  🎯 의심 구역     : {detect_count:>6,}개  ({detect_count/total_count*100:.1f}%)")
print(f"  ✅ 정상 구역     : {normal_count:>6,}개")
print(f"  ❌ 판독 실패     : {error_count:>6,}개")
print(f"{'─'*65}")
print(f"  📊 카테고리별 집계:")
for name in FIELD_NAMES:
    bar = "█" * min(category_counts[name], 30)
    print(f"     {name:<10}: {category_counts[name]:>4}건  {bar}")
print(f"{'─'*65}")
print(f"  📸 캡처 소요     : {capture_elapsed:>8.1f}초")
print(f"  🤖 AI 판독 소요  : {ai_elapsed:>8.1f}초")
print(f"  ⏱️  총 소요       : {total_elapsed:>8.1f}초")
print(f"{'─'*65}")
print(f"  💰 예상 비용     : ${total_count * 0.001 * 0.45:.4f} USD")
print(f"     (이미지 2장 전송으로 기존 대비 약 50% 증가)")
print(f"{'='*65}")
print("""
  📂 QGIS 활용 팁:
     ▸ 경작지만 보기  : 속성필터 → "경작지" = 1
     ▸ 복합위반 구역  : 속성필터 → "count" >= 2
     ▸ 카테고리별 색상: 스타일 → 단계구분 → count 필드
""")
print(f"{'='*65}")
