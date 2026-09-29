import { describe, expect, it } from "vitest";
import { apiBaseFor } from "./client";

describe("apiBaseFor", () => {
  it("uses the platform mount when the SPA is served at the service root", () => {
    // The platform app launcher opens the bare mount. An empty base here sent
    // /connect to the cluster root: ArangoDB's `unknown path '/connect'`.
    expect(apiBaseFor("/_service/uds/_db/AIM/arango-cypher-py/")).toBe(
      "/_service/uds/_db/AIM/arango-cypher-py",
    );
  });

  it("treats a mount opened without its trailing slash as a directory", () => {
    expect(apiBaseFor("/_service/uds/_db/AIM/arango-cypher-py")).toBe(
      "/_service/uds/_db/AIM/arango-cypher-py",
    );
  });

  it("drops an index.html page from the path", () => {
    expect(apiBaseFor("/_service/uds/_db/AIM/arango-cypher-py/index.html")).toBe(
      "/_service/uds/_db/AIM/arango-cypher-py",
    );
  });

  it("strips the /frontend and /ui sub-mounts to reach the API", () => {
    expect(apiBaseFor("/_service/uds/_db/d/app/frontend/")).toBe("/_service/uds/_db/d/app");
    expect(apiBaseFor("/_service/uds/_global/app/ui/index.html")).toBe("/_service/uds/_global/app");
    expect(apiBaseFor("/frontend/")).toBe("");
    expect(apiBaseFor("/ui")).toBe("");
  });

  it("is empty for local dev at the origin root", () => {
    expect(apiBaseFor("/")).toBe("");
    expect(apiBaseFor("")).toBe("");
  });

  it("compares whole segments, not substrings", () => {
    // The old indexOf("/ui") matched an instance named `ui-demo` and cut the
    // mount in half.
    expect(apiBaseFor("/_service/uds/_db/d/ui-demo/")).toBe("/_service/uds/_db/d/ui-demo");
    expect(apiBaseFor("/_service/uds/_db/frontend-db/app/")).toBe("/_service/uds/_db/frontend-db/app");
  });
});
