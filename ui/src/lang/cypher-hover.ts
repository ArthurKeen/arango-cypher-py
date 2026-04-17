import { hoverTooltip, type Tooltip } from "@codemirror/view";

interface DocEntry {
  summary: string;
  example: string;
}

const KEYWORD_DOCS: Record<string, DocEntry> = {
  MATCH: {
    summary: "Find patterns in the graph by specifying node and relationship shapes.",
    example: "MATCH (n:Person)-[:KNOWS]->(m)\nRETURN n, m",
  },
  WHERE: {
    summary: "Filter results by a boolean condition.",
    example: "MATCH (n:Person)\nWHERE n.age > 30\nRETURN n",
  },
  RETURN: {
    summary: "Define what data to output from the query.",
    example: "MATCH (n) RETURN n.name, n.age",
  },
  WITH: {
    summary: "Pass results between query parts, enabling chained transformations.",
    example: "MATCH (n) WITH n, n.age AS age\nWHERE age > 25\nRETURN n",
  },
  "OPTIONAL MATCH": {
    summary: "Like MATCH, but returns NULL for non-matching patterns instead of filtering rows.",
    example: "MATCH (n:Person)\nOPTIONAL MATCH (n)-[:OWNS]->(c:Car)\nRETURN n.name, c",
  },
  OPTIONAL: {
    summary: "Used with MATCH to return NULL for non-matching patterns instead of filtering.",
    example: "OPTIONAL MATCH (n)-[:KNOWS]->(m)\nRETURN n, m",
  },
  "ORDER BY": {
    summary: "Sort results by one or more expressions (ASC or DESC).",
    example: "MATCH (n:Person)\nRETURN n.name\nORDER BY n.name ASC",
  },
  ORDER: {
    summary: "Used with BY to sort results. ORDER BY expr ASC|DESC.",
    example: "RETURN n ORDER BY n.age DESC",
  },
  LIMIT: {
    summary: "Restrict the number of returned rows.",
    example: "MATCH (n) RETURN n LIMIT 10",
  },
  CREATE: {
    summary: "Create new nodes and relationships in the graph.",
    example: "CREATE (n:Person {name: 'Alice', age: 30})",
  },
  SET: {
    summary: "Update properties on nodes or relationships.",
    example: "MATCH (n {name: 'Alice'})\nSET n.age = 31",
  },
  DELETE: {
    summary: "Remove nodes or relationships (use DETACH DELETE for nodes with relationships).",
    example: "MATCH (n:Temp) DETACH DELETE n",
  },
  MERGE: {
    summary: "Match existing or create new patterns. Idempotent upsert.",
    example: "MERGE (n:Person {name: 'Alice'})\nON CREATE SET n.created = timestamp()",
  },
  UNWIND: {
    summary: "Expand a list into individual rows.",
    example: "UNWIND [1, 2, 3] AS x\nRETURN x",
  },
  CASE: {
    summary: "Conditional expression. Simple or generic form.",
    example: "RETURN CASE n.type\n  WHEN 'A' THEN 1\n  ELSE 0\nEND",
  },
  UNION: {
    summary: "Combine results from multiple queries (removes duplicates). Use UNION ALL to keep them.",
    example: "MATCH (n:Person) RETURN n.name\nUNION\nMATCH (n:Company) RETURN n.name",
  },
  CALL: {
    summary: "Invoke a procedure and optionally YIELD specific output columns.",
    example: "CALL db.labels() YIELD label\nRETURN label",
  },
  YIELD: {
    summary: "Select output columns from a CALL procedure invocation.",
    example: "CALL db.propertyKeys() YIELD propertyKey\nRETURN propertyKey",
  },
  DISTINCT: {
    summary: "Remove duplicate rows from results.",
    example: "MATCH (n) RETURN DISTINCT n.type",
  },
  AS: {
    summary: "Alias an expression in RETURN, WITH, or YIELD.",
    example: "RETURN n.name AS personName",
  },
  DETACH: {
    summary: "Used with DELETE to remove a node and all its relationships.",
    example: "MATCH (n:Old) DETACH DELETE n",
  },
  SKIP: {
    summary: "Skip a number of rows before returning results (pagination).",
    example: "MATCH (n) RETURN n SKIP 10 LIMIT 5",
  },
};

const FUNCTION_DOCS: Record<string, DocEntry> = {
  count: {
    summary: "Count the number of non-null values or rows.",
    example: "MATCH (n:Person) RETURN count(n)",
  },
  sum: {
    summary: "Sum numeric values in a group.",
    example: "MATCH (n:Order) RETURN sum(n.total)",
  },
  avg: {
    summary: "Average of numeric values in a group.",
    example: "MATCH (n:Person) RETURN avg(n.age)",
  },
  min: {
    summary: "Minimum value in a group.",
    example: "MATCH (n:Product) RETURN min(n.price)",
  },
  max: {
    summary: "Maximum value in a group.",
    example: "MATCH (n:Product) RETURN max(n.price)",
  },
  collect: {
    summary: "Aggregate values into a list.",
    example: "MATCH (n:Person) RETURN collect(n.name)",
  },
  size: {
    summary: "Return the number of elements in a list or the length of a string.",
    example: "MATCH (n) RETURN size(collect(n))\nRETURN size('hello')  // 5",
  },
  toLower: {
    summary: "Convert a string to lowercase.",
    example: "RETURN toLower('Hello')  // 'hello'",
  },
  toUpper: {
    summary: "Convert a string to uppercase.",
    example: "RETURN toUpper('Hello')  // 'HELLO'",
  },
  coalesce: {
    summary: "Return the first non-null value from arguments.",
    example: "RETURN coalesce(n.nickname, n.name)",
  },
  type: {
    summary: "Return the type string of a relationship.",
    example: "MATCH ()-[r]->() RETURN type(r)",
  },
  id: {
    summary: "Return the internal ID of a node or relationship.",
    example: "MATCH (n) RETURN id(n)",
  },
  labels: {
    summary: "Return a list of labels on a node.",
    example: "MATCH (n) RETURN labels(n)",
  },
  keys: {
    summary: "Return a list of property names of a node or relationship.",
    example: "MATCH (n:Person) RETURN keys(n)",
  },
  properties: {
    summary: "Return all properties of a node/relationship as a map.",
    example: "MATCH (n:Person) RETURN properties(n)",
  },
  toString: {
    summary: "Convert a value to a string.",
    example: "RETURN toString(123)  // '123'",
  },
  toInteger: {
    summary: "Convert a value to an integer.",
    example: "RETURN toInteger('42')  // 42",
  },
  toFloat: {
    summary: "Convert a value to a float.",
    example: "RETURN toFloat('3.14')  // 3.14",
  },
  head: {
    summary: "Return the first element of a list.",
    example: "RETURN head([1, 2, 3])  // 1",
  },
  tail: {
    summary: "Return all but the first element of a list.",
    example: "RETURN tail([1, 2, 3])  // [2, 3]",
  },
  last: {
    summary: "Return the last element of a list.",
    example: "RETURN last([1, 2, 3])  // 3",
  },
  length: {
    summary: "Return the length of a path or list.",
    example: "MATCH p = (a)-[*]->(b)\nRETURN length(p)",
  },
  nodes: {
    summary: "Return a list of nodes in a path.",
    example: "MATCH p = (a)-[*]->(b)\nRETURN nodes(p)",
  },
  relationships: {
    summary: "Return a list of relationships in a path.",
    example: "MATCH p = (a)-[*]->(b)\nRETURN relationships(p)",
  },
  range: {
    summary: "Generate a list of integers from start to end (inclusive).",
    example: "RETURN range(1, 5)  // [1, 2, 3, 4, 5]",
  },
  replace: {
    summary: "Replace occurrences of a substring.",
    example: "RETURN replace('hello', 'l', 'r')  // 'herro'",
  },
  substring: {
    summary: "Extract a substring by start index and optional length.",
    example: "RETURN substring('hello', 1, 3)  // 'ell'",
  },
  trim: {
    summary: "Remove leading and trailing whitespace.",
    example: "RETURN trim('  hello  ')  // 'hello'",
  },
  split: {
    summary: "Split a string by a delimiter into a list.",
    example: "RETURN split('a,b,c', ',')  // ['a', 'b', 'c']",
  },
  reverse: {
    summary: "Reverse a list or string.",
    example: "RETURN reverse([1, 2, 3])  // [3, 2, 1]",
  },
  abs: {
    summary: "Return the absolute value.",
    example: "RETURN abs(-5)  // 5",
  },
};

function getWordAt(doc: string, pos: number): { word: string; from: number; to: number } | null {
  if (pos < 0 || pos > doc.length) return null;
  let start = pos;
  let end = pos;
  while (start > 0 && /[a-zA-Z_]/.test(doc[start - 1])) start--;
  while (end < doc.length && /[a-zA-Z_]/.test(doc[end])) end++;
  if (start === end) return null;
  return { word: doc.slice(start, end), from: start, to: end };
}

export const cypherHoverTooltip = hoverTooltip((view, pos): Tooltip | null => {
  const doc = view.state.doc.toString();
  const hit = getWordAt(doc, pos);
  if (!hit) return null;

  const upper = hit.word.toUpperCase();
  const lower = hit.word.toLowerCase();

  let entry = KEYWORD_DOCS[upper] || FUNCTION_DOCS[lower];

  if (!entry && upper === "OPTIONAL") {
    const after = doc.slice(hit.to).match(/^\s+MATCH/i);
    if (after) entry = KEYWORD_DOCS["OPTIONAL MATCH"];
  }
  if (!entry && upper === "ORDER") {
    const after = doc.slice(hit.to).match(/^\s+BY/i);
    if (after) entry = KEYWORD_DOCS["ORDER BY"];
  }

  if (!entry) return null;

  return {
    pos: hit.from,
    end: hit.to,
    above: true,
    create() {
      const dom = document.createElement("div");
      dom.className = "cm-cypher-hover";
      dom.style.cssText = "max-width:380px;font-size:12px;line-height:1.4;padding:8px 10px;";

      const title = document.createElement("div");
      title.style.cssText = "font-weight:700;color:#c084fc;margin-bottom:4px;font-family:monospace;";
      title.textContent = FUNCTION_DOCS[lower] ? `${hit.word}()` : upper;
      dom.appendChild(title);

      const desc = document.createElement("div");
      desc.style.cssText = "color:#d1d5db;margin-bottom:6px;";
      desc.textContent = entry!.summary;
      dom.appendChild(desc);

      const code = document.createElement("pre");
      code.style.cssText = "background:#1e293b;padding:6px 8px;border-radius:4px;font-size:11px;color:#94a3b8;white-space:pre-wrap;margin:0;font-family:monospace;";
      code.textContent = entry!.example;
      dom.appendChild(code);

      return { dom };
    },
  };
});
