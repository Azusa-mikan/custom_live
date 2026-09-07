import uvicorn


def main():
    uvicorn.run(
        "src.api:app",
        host="0.0.0.0",
        port=8000,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
