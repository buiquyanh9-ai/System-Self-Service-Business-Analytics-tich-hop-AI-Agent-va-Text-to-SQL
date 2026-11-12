FROM python:3.12-slim
WORKDIR /app
ENV PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1 PYTHONUTF8=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Dữ liệu giả của cả hai doanh nghiệp mẫu được sinh ngay lúc build image (scale nhỏ hơn cho gói miễn phí).
# Chọn doanh nghiệp lúc chạy bằng biến WORKSPACE: để trống = ABC, WORKSPACE=giaovan_xyz = Giao vận XYZ.
ARG DATA_SCALE=0.5
RUN python warehouse/generate.py --scale ${DATA_SCALE} && python warehouse/build.py \
 && python workspaces/giaovan_xyz/warehouse/generate.py --scale ${DATA_SCALE} \
 && python -m app.onboard giaovan_xyz --stats
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
