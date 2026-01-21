from fastapi import FastAPI
import uvicorn

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/webhooks/gmail")
async def gmail_webhook():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
    