# 기반 이미지는 다이제스트로 고정한다. 갱신은 Dependabot 이 PR 로 올린다
FROM python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e

# OMP_THREAD_LIMIT: 워커 여럿이 한 호스트에서 돌 때 tesseract 가 코어를 독차지하지 않게 한다
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_THREAD_LIMIT=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 증빙 서류 OCR(objection_agent/ocr.py): Tesseract 한국어, 영어 데이터와 PDF 쪽 변환용 poppler
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kor poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --chmod=a=r requirements.txt .
RUN pip install --require-hashes -r requirements.txt

# 프롬프트(objection_agent/prompts.py)는 git-crypt 로 암호화돼 있다. 실행용 이미지는 잠금을 푼 작업 사본에서만 빌드하고,
# 그 이미지는 공개 레지스트리에 올리지 않는다(SECURITY.md)
COPY --chmod=a=rX objection_agent ./objection_agent

# 쓰기 권한 없는 사용자로 돈다. 키는 실행 시 환경변수로만 받는다.
# 실행 옵션(README 「운영 실행」): --read-only --tmpfs /tmp:size=512m,mode=1777 --memory 2g --cap-drop ALL
# --security-opt no-new-privileges. 서류 원본은 /tmp 에만 잠깐 놓이고, tmpfs 라 컨테이너가 죽어도 디스크에 남지 않는다
USER 65534:65534
CMD ["python", "-m", "objection_agent"]
