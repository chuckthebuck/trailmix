// Apply the roster to the already-running app without a code deployment.
// Open https://trailmix-ujeh.onrender.com/ in your browser, open DevTools > Console,
// paste this entire file, and press Enter. Run on the app's own page.
// Uses existing roster APIs. It creates missing people and sets the three Safety
// assignments; other matched class members are left unassigned. Existing
// non-class operators and all bag/history records are retained.
(async () => {
  const names = ["Truus", "Leslie", "Blake", "David", "Brian", "Aiden", "Megan", "Fabio", "Katelyn", "Elias", "Halen", "Dylan", "Luke", "Hailey", "Marie", "Keira", "Natalie", "Charlie", "Beaton", "Veena", "Nico", "Aum", "Alexander", "Martin", "Logan", "Lucas", "Vito", "Sarah"];
  const safety = new Set(['lucas','blake','charlie']);
  async function read() {
    const response = await fetch('/api/state');
    const body = await response.json();
    if (!response.ok) throw Error(body.error || 'Open the app and connect first.');
    return body;
  }
  let state = await read();
  function match(name) {
    const matches = state.people.filter(person => person.name.split(/\s+/)[0].toLowerCase() === name.toLowerCase());
    if (matches.length > 1) throw Error('Multiple operators match ' + name + '. Rename duplicates first.');
    if (matches[0]?.deletedAt) throw Error(name + ' is deleted. Restore them before importing.');
    return matches[0];
  }
  names.forEach(match);
  async function post(action, args) {
    const request = {action,args,operator:'Roster setup',requestId:'roster-'+crypto.randomUUID(),runId:state.runId,revision:state.revision};
    const response = await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-TrailMix':'1'},body:JSON.stringify(request)});
    const body = await response.json();
    if (!response.ok) throw Error(body.error || 'Save not confirmed; reload the app and rerun this script to check the roster.');
    state = body.state;
  }
  const missing = names.filter(name => !match(name));
  if (missing.length) await post('personAddMany',{names:missing,assignments:[]});
  for (const name of names) {
    const person = match(name);
    const roles = safety.has(name.toLowerCase()) ? ['safety'] : [];
    if (JSON.stringify(person.assignments) !== JSON.stringify(roles)) {
      await post('personUpdate',{person:person.id,expectedVersion:person.version,status:person.status,assignments:roles});
    }
  }
  console.table(names.map(name => {const p=match(name);return {operator:p.name,roles:p.assignments.join(', ')||'Unassigned'};}));
  console.log('Roster applied. Lucas, Blake and Charlie are Safety. Refresh the app if necessary.');
})().catch(error => console.error('Roster setup stopped:',error.message));
