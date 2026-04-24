FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY MVP_Clarity_4p_final.py .
EXPOSE 8080
ENV PORT=8080
CMD ["python", "MVP_Clarity_4p_final.py"]
