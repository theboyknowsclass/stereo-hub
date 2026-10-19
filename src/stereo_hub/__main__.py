import logging
import os

import uvicorn


def main() -> None:
    level = os.environ.get("LOG_LEVEL", "info").upper()
    # uvicorn only configures its own loggers; this makes ours and
    # aiolibresync's visible in `docker logs` too.
    logging.basicConfig(level=level, format="%(levelname)s:     %(name)s: %(message)s")
    uvicorn.run(
        "stereo_hub.app:app",
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_level=level.lower(),
    )


if __name__ == "__main__":
    main()
