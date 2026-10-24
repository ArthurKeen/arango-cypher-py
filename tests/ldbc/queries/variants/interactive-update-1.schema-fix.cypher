// VARIANT of interactive_v2/interactive-update-1.cypher (INS1), reason: schema-fix.
// Upstream creates [:WORKS_AT] and a Person property `languages`; the LDBC schema
// (and every read query) uses WORK_AT and `speaks`. Neo4j accepts both silently,
// so the upstream insert writes data the reads never see. Only those two tokens differ.
MATCH (c:City {id: $cityId})
CREATE (p:Person {
    id: $personId,
    firstName: $personFirstName,
    lastName: $personLastName,
    gender: $gender,
    birthday: $birthday,
    creationDate: $creationDate,
    locationIP: $locationIP,
    browserUsed: $browserUsed,
    speaks: $languages,
    email: $emails
  })-[:IS_LOCATED_IN]->(c)
WITH p, count(*) AS dummy1
UNWIND $tagIds AS tagId
  MATCH (t:Tag {id: tagId})
  CREATE (p)-[:HAS_INTEREST]->(t)
WITH p, count(*) AS dummy2
UNWIND $studyAt AS s
  MATCH (u:University {id: s[0]})
  CREATE (p)-[:STUDY_AT {classYear: s[1]}]->(u)
WITH p, count(*) AS dummy3
UNWIND $workAt AS w
  MATCH (comp:Company {id: w[0]})
  CREATE (p)-[:WORK_AT {workFrom: w[1]}]->(comp)
