const $ = id => document.getElementById(id);
const state = {points:[],places:[],reminders:[],events:[],day:null,lastEventId:0,insights:null,followToday:true,focusPlace:null,coverage:null,current:null};
let map, pathLayer, placeLayer, currentMarker, searchLayer;
let authDismissed=false;
let exploreRequest=0;

function toast(message){const box=$('toast');box.textContent=message;box.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>box.classList.remove('show'),3800)}
function safe(text){return String(text??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function formatTime(value){return value?new Date(value).toLocaleString(): '—'}
function modeName(mode){return mode==='running_cycling'?'Running / cycling':mode?mode[0].toUpperCase()+mode.slice(1):'Unknown'}
function kmh(point){return point?.speed_mps==null?'—':`${(point.speed_mps*3.6).toFixed(1)} km/h`}
function localDateKey(date=new Date()){return [date.getFullYear(),String(date.getMonth()+1).padStart(2,'0'),String(date.getDate()).padStart(2,'0')].join('-')}
function params(){const [year,month,day]=$('day-date').value.split('-').map(Number);const start=new Date(year,month-1,day);const end=new Date(year,month-1,day+1);const q=new URLSearchParams({start:start.toISOString(),end:new Date(end.getTime()-1).toISOString()});if($('source').value)q.set('source',$('source').value);return q}
function openAuth(message=''){if(authDismissed)return;$('auth-error').textContent=message;$('auth-panel').hidden=false;$('auth-token').focus()}
async function api(path,options={}){
  const previousToken=sessionStorage.getItem('geotracker-token');
  const headers={...(options.body?{'Content-Type':'application/json'}:{}),...(previousToken?{'Authorization':'Bearer '+previousToken}:{}),...(options.headers||{})};
  const response=await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store'});
  if(response.status===401){sessionStorage.removeItem('geotracker-token');$('connection').textContent='Sign in required';openAuth(previousToken?'The previously saved token was rejected. Enter the token used by the running server.':'Enter the access token to view your location history.');throw Error(previousToken?'Previously saved access token was rejected.':'Sign in required.')}
  const value=await response.json();if(!response.ok)throw Error(value.error||`HTTP ${response.status}`);return value;
}
async function post(path,body){return api(path,{method:'POST',body:JSON.stringify(body)})}
function showView(view){document.querySelectorAll('.view').forEach(el=>el.classList.toggle('active',el.id===view+'-view'));document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===view));if(view==='explore'&&map)setTimeout(()=>map.invalidateSize(),20)}
function initMap(){if(!window.L){$('map').innerHTML='<div class="empty" style="padding:30px">Map library could not load. Check your internet connection.</div>';return}map=L.map('map',{zoomControl:false}).setView([39.5,-98.35],4);L.control.zoom({position:'bottomright'}).addTo(map);L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OpenStreetMap contributors',maxZoom:19}).addTo(map);pathLayer=L.layerGroup().addTo(map);placeLayer=L.layerGroup().addTo(map);searchLayer=L.layerGroup().addTo(map);map.on('click',async e=>{const q=params();q.set('lat',e.latlng.lat);q.set('lon',e.latlng.lng);q.set('radius_m',250);q.set('limit',10);try{const rows=await api('/api/near?'+q);$('map-detail-section').hidden=false;$('map-detail').innerHTML=rows.map(p=>`<div class="list-item"><div><strong>${safe(formatTime(p.timestamp))}</strong><small>${safe(modeName(p.mode))} · ${safe(kmh(p))} · ${Math.round(p.distance_m)} m away</small></div></div>`).join('')||'<p class="empty">No recorded fixes within 250 m in this view.</p>'}catch(err){toast(err.message)}})}
function drawMap(){
  if(!map)return;
  pathLayer.clearLayers();
  placeLayer.clearLayers();
  if(currentMarker){map.removeLayer(currentMarker);currentMarker=null}
  const day=state.day;
  const bounds=[];
  for(const segment of day?.route_segments||[]){
    const coordinates=segment.map(c=>[c[1],c[0]]);
    L.polyline(coordinates,{color:'#176d69',weight:4,opacity:.82}).addTo(pathLayer);
    for(const coordinate of coordinates)bounds.push(coordinate);
  }
  for(const place of state.places){
    L.circle([place.lat,place.lon],{radius:place.radius_m,color:'#9aaba5',weight:1,fillOpacity:.04})
      .bindPopup('<strong>'+safe(place.name)+'</strong><br>Saved place').addTo(placeLayer);
  }
  for(const [index,place] of (day?.visited_places||[]).entries()){
    const icon=L.divIcon({className:'visit-pin',html:String(index+1),iconSize:[28,28],iconAnchor:[14,14]});
    L.marker([place.lat,place.lon],{icon,title:place.name||'Unlabeled stop'}).bindPopup('<strong>'+safe(place.name||'Unlabeled stop')+'</strong><br>'+place.visits+' observed visit'+(place.visits===1?'':'s')).addTo(placeLayer);
    bounds.push([place.lat,place.lon]);
  }
  if(day?.last_location){
    const point=day.last_location;
    currentMarker=L.circleMarker([point.lat,point.lon],{radius:6,color:'#fff',weight:2,fillColor:'#176d69',fillOpacity:1})
      .bindPopup('<strong>Last fix</strong><br>'+safe(formatTime(day.last_fix))).addTo(map);
    bounds.push([point.lat,point.lon]);
  }
  if(day?.first_location)bounds.push([day.first_location.lat,day.first_location.lon]);
  if(bounds.length)map.fitBounds(L.latLngBounds(bounds).pad(.15),{maxZoom:15});
  else if(state.places.length)map.fitBounds(L.latLngBounds(state.places.map(p=>[p.lat,p.lon])).pad(.2),{maxZoom:13});
}
function renderChart(){const points=[...state.points].reverse().filter(p=>p.speed_mps!=null).slice(-100);const box=$('speed-chart');if(points.length<2){box.innerHTML='<span class="empty">More timed samples are needed for a speed chart.</span>';return}const top=Math.max(10,...points.map(p=>p.speed_mps*3.6));const poly=points.map((p,i)=>`${(i/(points.length-1)*600).toFixed(1)},${(110-p.speed_mps*3.6/top*94).toFixed(1)}`).join(' ');box.innerHTML=`<svg viewBox="0 0 600 125" preserveAspectRatio="none" aria-label="Speed chart"><line x1="0" y1="110" x2="600" y2="110" stroke="#dce6eb"/><line x1="0" y1="63" x2="600" y2="63" stroke="#edf2f5"/><polyline fill="none" stroke="#177c91" stroke-width="3" points="${poly}"/><text x="0" y="12" fill="#98a7b4" font-size="10">${top.toFixed(0)} km/h</text><text x="0" y="123" fill="#98a7b4" font-size="10">${safe(formatTime(points[0].timestamp))}</text></svg>`}
function renderExplore(day,coverage){
  const hasHistory=day.points>0;
  const hasArchive=coverage.sources.some(source=>source.points>0);
  const hasMapContent=hasHistory||!!state.focusPlace;
  const [year,month,dayNumber]=day.date.split('-').map(Number);
  const chosen=new Date(year,month-1,dayNumber,12);
  $('history-title').textContent=day.date===localDateKey()?'Today':new Intl.DateTimeFormat(undefined,{weekday:'long',month:'long',day:'numeric'}).format(chosen);
  $('source-filter').hidden=coverage.sources.length<2;
  $('next-day').disabled=day.date>=localDateKey();
  $('today').disabled=day.date===localDateKey();
  const empty=$('history-empty');
  empty.querySelector('strong').textContent=hasArchive?'No fixes for this day':'No locations recorded';
  empty.querySelector('span').textContent=hasArchive?'Choose another date to explore your history.':'Connect a location source to see your history here.';
  $('summary').hidden=!hasArchive;
  $('history-details').hidden=!hasHistory;
  $('inspector').hidden=!hasHistory;
  $('history-empty').hidden=hasMapContent;
  $('map-footer').hidden=!day.route_segments.length;
  $('export').hidden=!hasHistory;
  $('locate').hidden=!day.last_location;
  $('history-layout').classList.toggle('no-data',!hasMapContent);
  $('history-layout').classList.toggle('map-only',!hasHistory);
  $('stat-driving').textContent=day.activity_counts.driving.toLocaleString();
  $('stat-walking').textContent=day.activity_counts.walking.toLocaleString();
  $('stat-running-cycling').textContent=day.activity_counts.running_cycling.toLocaleString();
  $('stat-places').textContent=day.visited_places.length.toLocaleString();
  $('stat-points').textContent=day.unique_points.toLocaleString()+' unique out of '+day.points.toLocaleString();
  $('day-context').hidden=!hasHistory;
  if(hasHistory){
    const first=new Date(day.first_fix).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
    const last=new Date(day.last_fix).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
    $('day-context').textContent=day.stats.distance_km.toFixed(1)+' km sampled · '+(day.points===1?'Fix at '+first:'Fixes from '+first+' to '+last)+' · Gaps are unobserved';
  }
  $('map-note').textContent=day.route_segments.length?day.route_segments.length+' recorded path segment'+(day.route_segments.length===1?'':'s'):hasHistory?day.unique_points.toLocaleString()+' selected fixes':state.focusPlace?'Saved place: '+state.focusPlace.name:'Recorded route';
  $('visited-places').innerHTML=day.visited_places.map((place,index)=>
    '<button class="visit-row" data-visit="'+index+'"><span class="visit-number">'+(index+1)+'</span><span><strong>'+safe(place.name||'Unlabeled stop')+'</strong><small>'+safe(new Date(place.first_seen).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}))+' · '+place.observed_minutes.toFixed(0)+' min observed'+(place.visits>1?' · '+place.visits+' visits':'')+'</small></span></button>'
  ).join('')||'<p class="empty">No confirmed stops on this day. Sparse fixes may miss a visit.</p>';
  $('visited-places').querySelectorAll('[data-visit]').forEach(button=>button.onclick=()=>{
    const place=day.visited_places[+button.dataset.visit];
    if(map){map.setView([place.lat,place.lon],16);document.querySelector('.map-shell').scrollIntoView({behavior:'smooth',block:'start'})}
  });
  $('map-detail-section').hidden=true;
  $('sample-count').textContent='Showing '+Math.min(state.points.length,100)+' of '+day.points.toLocaleString()+' fixes'+(state.points.length<day.points?' (map samples limited to '+state.points.length.toLocaleString()+')':'')+'.';
  $('points-table').innerHTML=state.points.slice(0,100).map(p=>'<tr><td>'+safe(formatTime(p.timestamp))+'</td><td>'+safe(p.source)+'</td><td><span class="mode-tag '+safe(p.mode)+'">'+safe(modeName(p.mode))+'</span></td><td>'+safe(kmh(p))+(p.speed_origin==='estimated'?' <small>est.</small>':'')+'</td><td>'+p.lat.toFixed(5)+', '+p.lon.toFixed(5)+'</td><td>'+(p.accuracy_m==null?'—':Math.round(p.accuracy_m)+' m')+'</td></tr>').join('')||'<tr><td colspan="6" class="empty">No fixes on this day.</td></tr>';
  $('trips').innerHTML=day.trips.map((trip,index)=>'<div class="list-item"><div><strong>'+safe(modeName(trip.mode))+' · '+trip.distance_km.toFixed(1)+' km</strong><small>'+safe(formatTime(trip.start))+' → '+safe(formatTime(trip.end))+'</small></div><button data-trip="'+index+'">Show on map</button></div>').join('')||'<p class="empty">No confirmed movement segments on this day.</p>';
  $('trips').querySelectorAll('[data-trip]').forEach(button=>button.onclick=()=>{
    const trip=day.trips[+button.dataset.trip];
    if(!map)return;
    drawMap();
    const line=L.polyline(trip.coordinates.map(c=>[c[1],c[0]]),{color:'#e39659',weight:6,opacity:.9}).addTo(pathLayer);
    map.fitBounds(line.getBounds().pad(.2));
    document.querySelector('.map-shell').scrollIntoView({behavior:'smooth',block:'start'});
  });
  renderChart();
  drawMap();
  if(map)setTimeout(()=>{map.invalidateSize();drawMap()},20);
}
function fillPlaceSelectors(){const select=$('reminder-place'),value=select.value;select.innerHTML='<option value="">Select a place</option>'+state.places.map(p=>`<option value="${p.id}">${safe(p.name)}</option>`).join('');select.value=value}
function renderPlaces(){fillPlaceSelectors();$('places-list').innerHTML=state.places.map(p=>`<div class="list-item"><div><strong>${safe(p.name)}</strong><small>${p.lat.toFixed(5)}, ${p.lon.toFixed(5)} · ${Math.round(p.radius_m)} m radius${p.address?' · '+safe(p.address):''}</small></div><div class="actions"><button data-focus="${p.id}">Map</button><button data-edit="${p.id}">Edit</button></div></div>`).join('')||'<div class="empty">No places saved yet. Add one using coordinates or map click.</div>';$('places-list').querySelectorAll('[data-focus]').forEach(b=>b.onclick=()=>{const p=state.places.find(x=>x.id===+b.dataset.focus);state.focusPlace=p;showView('explore');if(state.day&&state.coverage)renderExplore(state.day,state.coverage);setTimeout(()=>{map?.invalidateSize();map?.setView([p.lat,p.lon],15);document.querySelector('.map-shell').scrollIntoView({behavior:'smooth',block:'start'})},30)});$('places-list').querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>{const p=state.places.find(x=>x.id===+b.dataset.edit);$('place-id').value=p.id;$('place-name').value=p.name;$('place-lat').value=p.lat;$('place-lon').value=p.lon;$('place-radius').value=p.radius_m;$('place-address').value=p.address||'';window.scrollTo({top:0,behavior:'smooth'})})}
function renderReminders(){$('arrival-inbox').hidden=!state.events.length;$('reminders-list').innerHTML=state.reminders.map(r=>`<div class="list-item"><div><strong>${safe(r.text)}</strong><small>At ${safe(r.place_name)} · ${r.repeat?'Repeats':'Once'} · ${r.active?'Active':'Inactive'}</small></div><button data-reminder="${r.id}" data-active="${r.active}">${r.active?'Pause':'Enable'}</button></div>`).join('')||'<div class="empty">No reminders yet. Save a place, then create one here.</div>';$('reminders-list').querySelectorAll('[data-reminder]').forEach(b=>b.onclick=async()=>{try{await post('/api/reminders/active',{id:+b.dataset.reminder,active:b.dataset.active==='0'});await refreshMeta()}catch(e){toast(e.message)}});const unread=state.events.filter(e=>!e.acknowledged_at);$('event-count').hidden=!unread.length;$('event-count').textContent=unread.length;$('events-list').innerHTML=state.events.map(e=>`<div class="list-item ${e.acknowledged_at?'':'unread'}"><div><strong>${safe(e.text)}</strong><small>${safe(e.place_name)} · ${safe(formatTime(e.triggered_at))}</small></div>${e.acknowledged_at?'':`<button data-ack="${e.id}">Done</button>`}</div>`).join('')||'<div class="empty">Arrival events will appear here.</div>';$('events-list').querySelectorAll('[data-ack]').forEach(b=>b.onclick=async()=>{try{await post('/api/events/ack',{id:+b.dataset.ack});await refreshMeta()}catch(e){toast(e.message)}})}
function renderStays(rows){$('stays').innerHTML=rows.slice(0,10).map(s=>`<div class="list-item"><div><strong>${safe(s.place_name||'Unlabeled stop')} · ${s.observed_minutes.toFixed(0)} min</strong><small>${safe(formatTime(s.started_at))} · ${s.point_count} samples${s.ongoing?' · ongoing':''}</small></div><button data-stay="${s.id}">Map</button></div>`).join('')||'<div class="empty">No confirmed stays in this range. Two nearby samples spanning five minutes are needed.</div>';$('stays').querySelectorAll('[data-stay]').forEach(button=>button.onclick=()=>{const s=rows.find(row=>row.id===+button.dataset.stay);if(map){map.setView([s.lat,s.lon],16);document.querySelector('.map-shell').scrollIntoView({behavior:'smooth',block:'start'})}})}
function renderInsights(){const data=state.insights;if(!data)return;$('suggestions-panel').hidden=!data.unlabeled_places.length;const prediction=data.prediction;$('prediction').textContent=prediction.prediction?`Likely next from ${prediction.from}: ${prediction.prediction} (${Math.round(prediction.frequency*100)}% of ${prediction.observations} observed departures).${prediction.current_fix_stale?' Latest location is stale; verify before acting.':''}`:`Next place: insufficient evidence (${prediction.reason}).`;$('transitions').innerHTML=data.transitions.slice(0,8).map(t=>`<div class="list-item"><div><strong>${safe(t.from_name)} → ${safe(t.to_name)}</strong><small>${t.count} observed transition${t.count===1?'':'s'}</small></div></div>`).join('')||'<div class="empty">Patterns appear after visits to two or more stops.</div>';$('suggestions-list').innerHTML=data.unlabeled_places.slice(0,12).map((p,i)=>`<div class="list-item"><div><strong>${safe(p.address||'Unlabeled stop')} · ${p.visits} visit${p.visits===1?'':'s'}</strong><small>${p.observed_minutes.toFixed(0)} min observed · ${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}</small></div><button data-suggest="${i}">Name it</button></div>`).join('')||'<div class="empty">No unlabeled stop clusters yet.</div>';$('suggestions-list').querySelectorAll('[data-suggest]').forEach(button=>button.onclick=()=>{const p=data.unlabeled_places[+button.dataset.suggest];$('place-id').value='';$('place-name').value='';$('place-lat').value=p.lat;$('place-lon').value=p.lon;$('place-radius').value=75;$('place-address').value='';window.scrollTo({top:0,behavior:'smooth'});$('place-name').focus()})}
async function refreshCoverage(){const data=await api('/api/coverage');$('coverage').innerHTML=data.sources.map(s=>`<div class="list-item"><div><strong>${safe(s.source)} · ${s.points.toLocaleString()} fixes</strong><small>${safe(formatTime(s.first))} → ${safe(formatTime(s.last))}</small></div></div>`).join('')+(data.gaps_over_24h.length?`<p class="muted">${data.gaps_over_24h.length} gaps over 24 hours. Missing coverage cannot establish where you were.</p>`:'')||'<div class="empty">No source data yet.</div>'}
async function refreshInsights(){state.insights=await api('/api/insights?limit=50');renderInsights()}
async function refreshMeta(){[state.places,state.reminders,state.events]=await Promise.all([api('/api/places'),api('/api/reminders'),api('/api/events')]);renderPlaces();renderReminders();drawMap()}
async function refreshExplore(){
  const request=++exploreRequest;
  const date=$('day-date').value;
  const source=$('source').value;
  const timezone=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
  const dayQuery=new URLSearchParams({date,timezone});
  if(source)dayQuery.set('source',source);
  const query=params().toString();
  const [day,points,coverage]=await Promise.all([
    api('/api/day?'+dayQuery),api('/api/observations?'+query),api('/api/coverage')
  ]);
  if(request!==exploreRequest)return;
  const selected=$('source').value;
  $('source').innerHTML='<option value="">All sources</option>'+coverage.sources.map(item=>'<option value="'+safe(item.source)+'">'+safe(item.source)+'</option>').join('');
  $('source').value=selected;
  state.day=day;
  state.points=points;
  state.coverage=coverage;
  renderExplore(day,coverage);
  renderStays(day.stays);
}
async function refreshAll(){try{const [status,current]=await Promise.all([api('/api/status'),api('/api/current')]);state.current=current;$('connection').textContent='Connected';$('geocode-hint').textContent=status.geocoding?(current?'Results favor your last recorded location.':'Searches are worldwide until a location is recorded.'):'Address search needs a geocoding provider.';await refreshMeta();await Promise.all([refreshExplore(),refreshInsights(),refreshCoverage()])}catch(e){toast(e.message)}}
async function pollEvents(){try{const events=await api('/api/events?since_id='+state.lastEventId);const newest=events.reduce((max,e)=>Math.max(max,e.id),state.lastEventId);if(state.lastEventId&&events.length){for(const event of events.reverse()){toast(`${event.text} · ${event.place_name}`);if(Notification.permission==='granted')new Notification('Location reminder',{body:`${event.text} · ${event.place_name}`})}}state.lastEventId=newest;if(events.length)await refreshMeta()}catch(e){/* Connection errors are surfaced on explicit actions. */}}
$('auth-cancel').onclick=()=>{authDismissed=true;$('auth-panel').hidden=true;$('auth-token').value=''};
$('auth-form').onsubmit=async event=>{event.preventDefault();const token=$('auth-token').value.trim();try{const response=await fetch('/api/session',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const result=await response.json();if(!response.ok)throw Error(result.error||'Access token rejected');sessionStorage.removeItem('geotracker-token');$('auth-token').value='';$('auth-panel').hidden=true;authDismissed=false;await refreshAll()}catch(error){$('auth-error').textContent=error.message}};
document.querySelectorAll('.detail-tab').forEach(button=>button.onclick=()=>{const name=button.dataset.panel;document.querySelectorAll('.detail-tab').forEach(tab=>{const active=tab===button;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active))});document.querySelectorAll('.detail-panel').forEach(panel=>panel.classList.toggle('active',panel.dataset.panelView===name))});
function moveDay(offset){
  state.focusPlace=null;
  const [year,month,day]=$('day-date').value.split('-').map(Number);
  $('day-date').value=localDateKey(new Date(year,month-1,day+offset,12));
  state.followToday=$('day-date').value===localDateKey();
  refreshExplore().catch(error=>toast(error.message));
}
document.querySelectorAll('.nav').forEach(button=>button.onclick=()=>{if(button.dataset.view==='explore'){state.focusPlace=null;if(state.day&&state.coverage)renderExplore(state.day,state.coverage)}showView(button.dataset.view)});
$('auth-button').onclick=()=>{authDismissed=false;openAuth()};
$('day-date').onchange=()=>{state.focusPlace=null;state.followToday=$('day-date').value===localDateKey();refreshExplore().catch(error=>toast(error.message))};
$('previous-day').onclick=()=>moveDay(-1);
$('next-day').onclick=()=>moveDay(1);
$('today').onclick=()=>{state.focusPlace=null;$('day-date').value=localDateKey();state.followToday=true;refreshExplore().catch(error=>toast(error.message))};
$('source').onchange=()=>{state.focusPlace=null;refreshExplore().catch(error=>toast(error.message))};
$('locate').onclick=()=>{const point=state.day?.last_location;if(point&&map)map.setView([point.lat,point.lon],15)};
$('export').onclick=async()=>{try{const q=params().toString();const data=await api('/api/export.geojson'+(q?'?'+q:''));const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/geo+json'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='geotracker-export.geojson';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000)}catch(e){toast(e.message)}};
$('search-form').onsubmit=async e=>{e.preventDefault();try{const q=new URLSearchParams({q:$('search').value});state.current=await api('/api/current');if(state.current){q.set('lat',state.current.lat);q.set('lon',state.current.lon)}const results=await api('/api/geocode?'+q);$('search-results').innerHTML=results.map((p,i)=>`<div class="result-item"><div><strong>${safe(p.name)}</strong><span>${Number(p.lat).toFixed(5)}, ${Number(p.lon).toFixed(5)}</span></div><button data-search="${i}">Save</button></div>`).join('')||'<p class="empty">No matches.</p>';$('search-results').querySelectorAll('[data-search]').forEach(b=>b.onclick=()=>{const p=results[+b.dataset.search];$('place-id').value='';$('place-name').value=p.name.slice(0,120);$('place-lat').value=p.lat;$('place-lon').value=p.lon;$('place-address').value=p.address;showView('places');window.scrollTo({top:0,behavior:'smooth'})});if(map&&results[0]){searchLayer.clearLayers();L.marker([results[0].lat,results[0].lon]).addTo(searchLayer);map.setView([results[0].lat,results[0].lon],13)}}catch(err){toast(err.message)}};
$('place-form').onsubmit=async e=>{e.preventDefault();try{await post('/api/places',{...($('place-id').value?{id:+$('place-id').value}:{}),name:$('place-name').value,lat:+$('place-lat').value,lon:+$('place-lon').value,radius_m:+$('place-radius').value,address:$('place-address').value});$('place-form').reset();$('place-id').value='';$('place-radius').value=150;await refreshMeta();await Promise.all([refreshExplore(),refreshInsights()]);toast('Place saved')}catch(err){toast(err.message)}};
$('reverse').onclick=async()=>{if(!$('place-lat').value||!$('place-lon').value){toast('Enter coordinates first');return}try{const q=new URLSearchParams({lat:$('place-lat').value,lon:$('place-lon').value});const result=await api('/api/reverse?'+q);$('place-address').value=result.address||'';if(!$('place-name').value)$('place-name').value=(result.name||'').slice(0,120);toast(result.approximate?'Nearby cached address found — verify before saving':'Address found')}catch(err){toast(err.message)}};
$('day-date').value=localDateKey();
$('reminder-repeat').onchange=()=>{$('reminder-cooldown-field').hidden=!$('reminder-repeat').checked};$('reminder-form').onsubmit=async e=>{e.preventDefault();try{await post('/api/reminders',{text:$('reminder-text').value,place_id:+$('reminder-place').value,repeat:$('reminder-repeat').checked,cooldown_minutes:+$('reminder-cooldown').value});$('reminder-form').reset();$('reminder-cooldown').value=1440;$('reminder-cooldown-field').hidden=true;await refreshMeta();toast('Reminder created')}catch(err){toast(err.message)}};
$('notifications').onclick=async()=>{if(!('Notification' in window)){toast('Browser notifications are unavailable');return}const permission=await Notification.requestPermission();toast(permission==='granted'?'Browser alerts enabled':'Browser alerts were not enabled')};
initMap();refreshAll().then(()=>{state.lastEventId=state.events.reduce((max,e)=>Math.max(max,e.id),0)});setInterval(pollEvents,10000);setInterval(()=>{if(state.followToday&&$('day-date').value!==localDateKey())$('day-date').value=localDateKey();refreshExplore().catch(()=>{})},60000);setInterval(()=>refreshInsights().catch(()=>{}),300000);
