// Talks to the backend (/api). With ?mock=1 in the address the same calls are answered by
// mock.js (invented data, nothing leaves the browser), so the page can be tried without the API.

const params = new URLSearchParams(location.search);
export const isMock = params.get("mock") === "1";

let mockModule = null;

async function transport(path, options = {}) {
  if (isMock) {
    mockModule ??= await import("./mock.js");
    return mockModule.respond(path, options);
  }
  return fetch("/api" + path, options);
}

async function failure(response) {
  let detail = response.statusText || `Error ${response.status}`;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") detail = body.detail;
    else if (Array.isArray(body.detail)) detail = body.detail.map((d) => d.msg).join("; ");
  } catch {
    /* not JSON */
  }
  return new Error(detail);
}

/** JSON call; throws Error(detail) when the server says no. */
export async function api(path, options) {
  const response = await transport(path, options);
  if (!response.ok) throw await failure(response);
  return response.status === 204 ? null : response.json();
}

export const jsonBody = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/** A page of a document as an image: { blob, pages }. */
export async function pageImage(documentId, page, rotation, dpi) {
  const query = new URLSearchParams({ rotation: String(rotation), dpi: String(dpi) });
  const response = await transport(`/documents/${documentId}/page/${page}.png?${query}`);
  if (!response.ok) throw await failure(response);
  return { blob: await response.blob(), pages: Number(response.headers.get("X-Pages")) || null };
}

/** Plain links (original PDF, Excel) only exist with the real server. */
export const fileUrl = (path) => (isMock ? null : "/api" + path);
