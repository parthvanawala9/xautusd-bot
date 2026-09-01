export default async function handler(req, res) {
  const oracleUrl = process.env.ORACLE_DASHBOARD_URL;

  if (!oracleUrl) {
    return res.status(500).json({
      error: "ORACLE_DASHBOARD_URL is not configured"
    });
  }

  const target = new URL(req.url, "https://vercel.local");
  const path = target.pathname + target.search;

  const response = await fetch(`${oracleUrl}${path}`, {
    method: req.method,
    headers: {
      "Content-Type": req.headers["content-type"] || "",
    },
  });

  const body = await response.arrayBuffer();

  res.status(response.status);

  response.headers.forEach((value, key) => {
    if (!["content-encoding", "transfer-encoding", "content-length"].includes(key)) {
      res.setHeader(key, value);
    }
  });

  return res.send(Buffer.from(body));
}
