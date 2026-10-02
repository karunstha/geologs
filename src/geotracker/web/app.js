const $ = id => document.getElementById(id);
const state = {points:[],timeline:[],places:[],visitedPlaces:[],reminders:[],reminderHistory:[],events:[],dailySummaries:[],day:null,lastEventId:0,insights:null,settings:null,followToday:true,focusPlace:null,coverage:null,current:null,sampleView:'timeline',rawPage:0,rawPageSize:50,rawTotal:0,selected:null,fitKey:null,pendingStayId:null};
let map, pathLayer, placeLayer, highlightLayer, currentMarker, searchLayer;
let authDismissed=false;
let exploreRequest=0;

function toast(message){const box=$('toast');box.textContent=message;box.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>box.classList.remove('show'),3800)}
function safe(text){return String(text??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function zoneOptions(){const timeZone=state.settings?.timezone;if(!timeZone)return{};try{new Intl.DateTimeFormat(undefined,{timeZone});return{timeZone}}catch(_error){return{}}}
function formatTime(value){return value?new Date(value).toLocaleString(undefined,zoneOptions()): '—'}
function clock(value){return new Date(value).toLocaleTimeString([],{hour:'numeric',minute:'2-digit',...zoneOptions()})}
function durationText(minutes){const total=Math.round(minutes||0);if(total<1)return'<1 min';const hours=Math.floor(total/60),rest=total%60;return hours?`${hours} h${rest?` ${rest} min`:''}`:`${rest} min`}
function distanceText(km){return km>=10?`${km.toFixed(0)} km`:km>=.1?`${km.toFixed(1)} km`:`${Math.round(km*1000)} m`}
const modeInfo={
  driving:{label:'Drive',noun:'Driving',color:'#3567ad',icon:'<svg viewBox="0 0 24 24"><path d="M5 11l1.6-4.2A2 2 0 0 1 8.5 5.5h7a2 2 0 0 1 1.9 1.3L19 11"/><path d="M3.5 11h17v5.5h-17z"/><path d="M6 16.5v2M18 16.5v2"/><circle cx="7.5" cy="13.7" r=".6"/><circle cx="16.5" cy="13.7" r=".6"/></svg>'},
  cycling:{label:'Ride',noun:'Cycling',color:'#8352bd',icon:'<svg viewBox="0 0 24 24"><circle cx="6" cy="16" r="3.5"/><circle cx="18" cy="16" r="3.5"/><path d="M6 16l3.5-7h6L18 16M9.5 9l3 7H6M14 5.5h2.5l-1 3.5"/></svg>'},
  running:{label:'Run',noun:'Running',color:'#cf5f22',icon:'<svg viewBox="0 0 24 24"><circle cx="15.5" cy="4.5" r="1.8"/><path d="M5 20.5l3.5-.5 2.2-3.8M10.7 16.2l3.3 2.3v3M7 11.5l2.5-3.3 4.3.8 2.4 3.3 3 .7M10.8 16.2l2.9-7.2"/></svg>'},
  transit:{label:'Transit',noun:'Transit',color:'#a83f78',icon:'<svg viewBox="0 0 24 24"><rect x="5" y="3.5" width="14" height="14" rx="2.5"/><path d="M5 11h14M8 17.5v2.5M16 17.5v2.5"/><circle cx="8.5" cy="14.2" r=".7"/><circle cx="15.5" cy="14.2" r=".7"/></svg>'},
  walking:{label:'Walk',noun:'Walking',color:'#2d8a57',icon:'<svg viewBox="0 0 24 24"><circle cx="13" cy="4.5" r="1.8"/><path d="M10 21l1.8-6.2-2.3-2.3.9-4.3 3 .9 1.6 3 2.4.9M11.8 14.8l2.9 2.7.8 3.5M10.4 8.2L7.4 10v3"/></svg>'}
};
function modeName(mode){return mode==='running_cycling'?'Running / cycling':modeInfo[mode]?.noun||(mode?mode[0].toUpperCase()+mode.slice(1):'Unknown')}
function modeIcon(mode){const info=modeInfo[mode];return info?`<span class="mode-icon" style="--mode:${info.color}" aria-hidden="true">${info.icon}</span>`:''}
function stayActivityText(stay){if(stay.kind!=='area')return'';const distance=stay.distance_m?` · ${distanceText(stay.distance_m/1000)}`:'';return stay.activity?`${modeInfo[stay.activity]?.noun||stay.activity}${distance}${stay.activity_corrected?' · set by you':''}`:'Moving around'}
function roleName(role){return ({arrival:'Arrival',departure:'Departure',latest:'Latest at stop',movement:'Movement'})[role]||'Raw sample'}
function kmh(point){return point?.speed_mps==null?'—':`${(point.speed_mps*3.6).toFixed(1)} km/h`}
const placeIcons={
  shopping:'<svg viewBox="0 0 24 24"><path d="M3 4h2l2.2 10.2a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 2-1.6L20 8H7"/><circle cx="10" cy="20" r="1"/><circle cx="18" cy="20" r="1"/></svg>',
  salon:'<svg viewBox="0 0 24 24"><circle cx="6" cy="7" r="3"/><circle cx="6" cy="17" r="3"/><path d="m8.7 8.3 11.8 7.2M8.7 15.7 20.5 8.5"/></svg>',
  water:'<svg viewBox="0 0 24 24"><path d="M12 3s6 6.3 6 11a6 6 0 0 1-12 0c0-4.7 6-11 6-11Z"/><path d="M9 15c.7 1.5 1.8 2.2 3.5 2.2"/></svg>',
  education:'<svg viewBox="0 0 24 24"><path d="m3 10 9-5 9 5-9 5-9-5Z"/><path d="M7 12.5V17c3 2 7 2 10 0v-4.5M21 10v6"/></svg>',
  bar:'<svg viewBox="0 0 24 24"><path d="M5 4h14l-7 8-7-8Z"/><path d="M12 12v7M8 20h8"/></svg>',
  park:'<svg viewBox="0 0 24 24"><path d="M12 21V9M7 13c-3-4-.4-9 5-10 5.4 1 8 6 5 10-1.5 2-3.5 2.5-5 2.5S8.5 15 7 13Z"/></svg>',
  food:'<svg viewBox="0 0 24 24"><path d="M7 3v8M4 3v5c0 2 1 3 3 3s3-1 3-3V3M7 11v10M16 3v18M16 3c3 1 4 4 4 7h-4"/></svg>',
  health:'<svg viewBox="0 0 24 24"><path d="M12 21S4 16 4 9a4 4 0 0 1 7-2.7L12 7.5l1-1.2A4 4 0 0 1 20 9c0 7-8 12-8 12Z"/><path d="M9 12h6M12 9v6"/></svg>',
  home:'<svg viewBox="0 0 24 24"><path d="m3 11 9-8 9 8M5 10v11h14V10M9 21v-7h6v7"/></svg>',
  place:'<svg viewBox="0 0 24 24"><path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/></svg>'
};
function placeKind(place={}){const text=[place.category,...(place.categories||[]),place.name,place.display_name].join(' ').toLowerCase();if(/(^|\s)(home|my home)(\s|$)/.test(text))return'home';if(/walmart|supermarket|shopping|commercial|retail|convenience|grocery|department_store|shop\b|store\b/.test(text))return'shopping';if(/hair|salon|beauty|barber/.test(text))return'salon';if(/lake|water|reservoir|river|natural\.water/.test(text))return'water';if(/university|college|school|education|academic/.test(text))return'education';if(/bar|pub|nightclub|biergarten/.test(text))return'bar';if(/restaurant|cafe|coffee|food|catering|bakery|fast_food/.test(text))return'food';if(/park|garden|nature|leisure/.test(text))return'park';if(/hospital|clinic|pharmacy|healthcare|doctor|dentist|medical/.test(text))return'health';return'place'}
function placeIcon(place){const kind=placeKind(place);return `<span class="place-icon ${kind}" title="${safe(kind)}" aria-hidden="true">${placeIcons[kind]}</span>`}
function populateTimezones(selected){const select=$('summary-timezone');let zones=[];try{zones=Intl.supportedValuesOf('timeZone')}catch(_error){zones=['America/Chicago','America/Denver','America/Los_Angeles','America/New_York','America/Phoenix','America/Toronto','America/Vancouver','Europe/London','Europe/Paris','Asia/Kolkata','Asia/Tokyo','Australia/Sydney']};zones=[...new Set(['UTC',...zones,selected].filter(Boolean))];select.innerHTML='';const groups=new Map();for(const zone of zones){const area=zone.includes('/')?zone.split('/')[0]:'General';if(!groups.has(area))groups.set(area,[]);groups.get(area).push(zone)}for(const [area,items] of groups){const group=document.createElement('optgroup');group.label=area;for(const zone of items.sort()){const option=document.createElement('option');option.value=zone;option.textContent=zone.replaceAll('_',' ');group.appendChild(option)}select.appendChild(group)}select.value=selected||'UTC'}
function localDateKey(date=new Date()){
  const timezone=state.settings?.timezone;
  if(timezone){try{const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(date).map(part=>[part.type,part.value]));return `${parts.year}-${parts.month}-${parts.day}`}catch(_error){}}
  return [date.getFullYear(),String(date.getMonth()+1).padStart(2,'0'),String(date.getDate()).padStart(2,'0')].join('-');
}
function shiftDateKey(value,offset){const [year,month,day]=value.split('-').map(Number);const date=new Date(Date.UTC(year,month-1,day+offset));return [date.getUTCFullYear(),String(date.getUTCMonth()+1).padStart(2,'0'),String(date.getUTCDate()).padStart(2,'0')].join('-')}
function zonedMidnight(dateKey){
  // Midnight of dateKey in the configured timezone, not the browser's.
  const [year,month,day]=dateKey.split('-').map(Number);
  const target=Date.UTC(year,month-1,day);
  const timeZone=zoneOptions().timeZone;
  if(!timeZone)return new Date(year,month-1,day);
  const format=new Intl.DateTimeFormat('en-US',{timeZone,hourCycle:'h23',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  let guess=target;
  for(let step=0;step<3;step++){const parts=Object.fromEntries(format.formatToParts(new Date(guess)).map(part=>[part.type,part.value]));const shown=Date.UTC(+parts.year,+parts.month-1,+parts.day,+parts.hour%24,+parts.minute,+parts.second);guess+=target-shown}
  return new Date(guess);
}
function params(){const date=$('day-date').value;const start=zonedMidnight(date);const end=zonedMidnight(shiftDateKey(date,1));const q=new URLSearchParams({start:start.toISOString(),end:new Date(end.getTime()-1).toISOString()});if($('source').value)q.set('source',$('source').value);return q}
function openAuth(message=''){if(authDismissed)return;$('auth-error').textContent=message;$('auth-panel').hidden=false;$('auth-token').focus()}
async function api(path,options={}){
  const previousToken=sessionStorage.getItem('geotracker-token');
  const headers={...(options.body?{'Content-Type':'application/json'}:{}),...(previousToken?{'Authorization':'Bearer '+previousToken}:{}),...(options.headers||{})};
  const response=await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store'});
  if(response.status===401){sessionStorage.removeItem('geotracker-token');$('connection').textContent='Sign in required';openAuth(previousToken?'The previously saved token was rejected. Enter the token used by the running server.':'Enter the access token to view your location history.');throw Error(previousToken?'Previously saved access token was rejected.':'Sign in required.')}
  const value=await response.json();if(!response.ok)throw Error(value.error||`HTTP ${response.status}`);return value;
}
async function post(path,body){return api(path,{method:'POST',body:JSON.stringify(body)})}
function showView(view){document.querySelectorAll('.view').forEach(el=>el.classList.toggle('active',el.id===view+'-view'));document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===view));if(view==='explore'&&map)setTimeout(()=>map.invalidateSize(),20);if(view==='summaries')refreshSummaries().catch(error=>toast(error.message))}
function initMap(){
  if(!window.L){$('map').innerHTML='<div class="empty" style="padding:30px">Map library could not load. Check your internet connection.</div>';return}
  map=L.map('map',{zoomControl:false}).setView([39.5,-98.35],4);
  L.control.zoom({position:'bottomright'}).addTo(map);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OpenStreetMap contributors',maxZoom:19}).addTo(map);
  map.on('popupopen',event=>{
    const element=event.popup.getElement();
    element?.querySelectorAll('[data-copy]').forEach(button=>button.onclick=()=>copyText(button.dataset.copy,'Coordinates copied'));
    element?.querySelectorAll('[data-edit-place]').forEach(button=>button.onclick=()=>{map.closePopup();editVisitedPlace(state.day?.places_including_home.find(place=>place.cluster_id===+button.dataset.editPlace))});
    element?.querySelectorAll('[data-edit-stay]').forEach(button=>button.onclick=()=>{
      map.closePopup();
      const stay=state.day?.stays.find(item=>item.id===+button.dataset.editStay);
      if(stay)editVisitedPlace({...stay,name:stay.display_name},[stay]);
    });
  });
  pathLayer=L.layerGroup().addTo(map);placeLayer=L.layerGroup().addTo(map);highlightLayer=L.layerGroup().addTo(map);searchLayer=L.layerGroup().addTo(map);
  map.on('click',async e=>{const q=params();q.set('lat',e.latlng.lat);q.set('lon',e.latlng.lng);q.set('radius_m',250);q.set('limit',10);try{const rows=await api('/api/near?'+q);$('map-detail-section').hidden=false;$('map-detail').innerHTML=rows.map(p=>`<div class="list-item"><div><strong>${safe(formatTime(p.timestamp))}</strong><small>${safe(modeName(p.mode))} · ${safe(kmh(p))} · ${Math.round(p.distance_m)} m away · ${safe(coordinateText(p.lat,p.lon))}</small></div></div>`).join('')||'<p class="empty">No recorded fixes within 250 m on this day.</p>'}catch(err){toast(err.message)}});
}
function mapPath(segment){return segment.map(c=>[c[1],c[0]])}
function coordinateText(lat,lon){return `${Number(lat).toFixed(6)}, ${Number(lon).toFixed(6)}`}
function pointPopup(title,lines,lat,lon,edit=''){
  // Every point on the map shows its coordinates, ready to copy; visits can be renamed from here.
  const coordinates=coordinateText(lat,lon);
  return `<div class="map-popup"><strong>${safe(title)}</strong>${lines.filter(Boolean).map(line=>`<span>${safe(line)}</span>`).join('')}<div class="popup-actions"><button type="button" class="popup-copy" data-copy="${safe(coordinates)}" title="Copy coordinates">${safe(coordinates)}</button>${edit}</div></div>`;
}
// A pin stands for a place (all its visits); a selected stay is one visit's spot.
function editPlaceAction(clusterId){return `<button type="button" class="popup-action" data-edit-place="${clusterId}">Edit place</button>`}
function editStayAction(stayId){return `<button type="button" class="popup-action" data-edit-stay="${stayId}">Fix this visit's name</button>`}
async function copyText(value,label='Copied'){try{await navigator.clipboard.writeText(value);toast(label)}catch(_error){toast(value)}}
function tripKey(trip){return `${trip.from_stay_id}-${trip.to_stay_id}`}
function stayColor(stay){return modeInfo[stay.activity]?.color||'#176d69'}
function selectedItem(){
  const day=state.day,selected=state.selected;
  if(!day||!selected)return null;
  if(selected.type==='stay'){const stay=day.stays.find(item=>item.id===selected.key);return stay?{type:'stay',stay}:null}
  const trip=day.trips.find(item=>tripKey(item)===selected.key);return trip?{type:'trip',trip}:null;
}
function dayBounds(){
  const day=state.day,bounds=[];
  for(const segment of day?.route_segments||[])bounds.push(...mapPath(segment));
  for(const place of day?.places_including_home||[])bounds.push([place.lat,place.lon]);
  if(state.current&&(day?.date===localDateKey()||!day?.points))bounds.push([state.current.lat,state.current.lon]);
  return bounds;
}
function fitDay(){
  if(!map)return;
  const bounds=dayBounds();
  if(bounds.length)map.fitBounds(L.latLngBounds(bounds).pad(.12),{maxZoom:15});
  else if(state.places.length)map.fitBounds(L.latLngBounds(state.places.map(p=>[p.lat,p.lon])).pad(.2),{maxZoom:13});
}
function drawMap(fit=false){
  if(!map)return;
  pathLayer.clearLayers();placeLayer.clearLayers();
  if(currentMarker){map.removeLayer(currentMarker);currentMarker=null}
  const day=state.day;
  // The whole recorded path sits underneath; trips and area stays draw over it in their mode colour.
  for(const segment of day?.route_segments||[])L.polyline(mapPath(segment),{color:'#8ea8a2',weight:3,opacity:.7,interactive:false}).addTo(pathLayer);
  for(const trip of day?.trips||[]){
    for(const segment of trip.route_segments||[])L.polyline(mapPath(segment),{color:modeInfo[trip.mode]?.color||'#176d69',weight:4,opacity:.9}).on('click',()=>selectItem('trip',tripKey(trip))).addTo(pathLayer);
  }
  for(const stay of day?.stays||[]){
    for(const move of stay.internal_moves||[])for(const segment of move.route_segments||[])L.polyline(mapPath(segment),{color:modeInfo[move.mode]?.color||'#176d69',weight:3,opacity:.8,dashArray:'6 6'}).on('click',()=>selectItem('stay',stay.id)).addTo(pathLayer);
    if(stay.kind!=='area')continue;
    L.circle([stay.lat,stay.lon],{radius:stay.radius_m||50,color:stayColor(stay),weight:1,opacity:.45,fillOpacity:.05,interactive:false}).addTo(pathLayer);
    for(const segment of stay.route_segments||[])L.polyline(mapPath(segment),{color:stayColor(stay),weight:4,opacity:.9}).on('click',()=>selectItem('stay',stay.id)).addTo(pathLayer);
  }
  for(const place of state.places){
    L.circle([place.lat,place.lon],{radius:place.radius_m,color:'#9aaba5',weight:1,fillOpacity:.04})
      .bindPopup(pointPopup(place.name,['Saved place · '+Math.round(place.radius_m)+' m radius'],place.lat,place.lon)).addTo(placeLayer);
  }
  if(state.focusPlace){
    const label=state.focusPlace.name||state.focusPlace.display_name||'Observed place';
    L.circleMarker([state.focusPlace.lat,state.focusPlace.lon],{radius:9,color:'#fff',weight:3,fillColor:'#176d69',fillOpacity:1})
      .bindPopup(pointPopup(label,[state.focusPlace.timestamp?formatTime(state.focusPlace.timestamp):''],state.focusPlace.lat,state.focusPlace.lon)).addTo(placeLayer);
  }
  const numbers=new Map((day?.visited_places||[]).map((place,index)=>[place.cluster_id,index+1]));
  for(const place of day?.places_including_home||[]){
    const number=numbers.get(place.cluster_id);
    const icon=L.divIcon({className:'visit-pin'+(number?'':' home'),html:number?String(number):placeIcons.home,iconSize:[28,28],iconAnchor:[14,14]});
    const minutes=durationText(place.observed_minutes);
    L.marker([place.lat,place.lon],{icon,title:place.name||place.display_name})
      .bindPopup(pointPopup(place.name||place.display_name,[place.address&&place.address!==place.name?place.address:'',minutes+' · '+place.visits+' visit'+(place.visits===1?'':'s')],place.lat,place.lon,editPlaceAction(place.cluster_id))).addTo(placeLayer);
  }
  if(state.current){
    const point=state.current;
    currentMarker=L.circleMarker([point.lat,point.lon],{radius:7,color:'#fff',weight:3,fillColor:'#dc7650',fillOpacity:1})
      .bindPopup(pointPopup(point.place_name||point.address||'Current location',['Updated '+formatTime(point.timestamp)],point.lat,point.lon)).addTo(map);
  }
  if(fit)fitDay();
  drawSelection(false);
}
const TRIP_MODES=['walking','running','cycling','driving','transit'],AREA_MODES=['walking','running','cycling'];
function modeStrip(subject,options,current,corrected,detected){
  // subject: what the correction is for (source, target, start, end).
  const buttons=options.map(mode=>`<button type="button" class="mode-option${mode===current?' active':''}" style="--mode:${modeInfo[mode].color}" data-set-mode="${mode}" aria-pressed="${mode===current}">${modeIcon(mode)}${safe(modeInfo[mode].label)}</button>`).join('');
  const note=corrected?`<span class="mode-note">Detected: ${safe(modeName(detected))}</span><button type="button" class="text-button" data-set-mode="auto">Automatic</button>`:'<span class="mode-note">Detected from speed</span>';
  return `<li class="tl-mode-picker" data-mode-subject='${safe(JSON.stringify(subject))}'><div class="mode-options">${buttons}</div><div class="mode-meta">${note}</div></li>`;
}
function renderModePicker(){
  document.querySelectorAll('#day-timeline .tl-mode-picker').forEach(item=>item.remove());
  const item=selectedItem();
  if(!item)return;
  const row=document.querySelector(`#day-timeline [data-item="${state.selected.type}:${state.selected.key}"]`)?.closest('li');
  if(!row)return;
  let html='';
  if(item.type==='trip'){
    const trip=item.trip;
    html=modeStrip({source:trip.source,target:'trip',started_at:trip.start,ended_at:trip.end},TRIP_MODES,trip.mode,trip.mode_corrected,trip.detected_mode);
  }else{
    const stay=item.stay;
    if(stay.kind==='area')html+=modeStrip({source:stay.source,target:'stay',started_at:stay.started_at,ended_at:stay.ended_at},AREA_MODES,stay.activity,stay.activity_corrected,stay.detected_activity);
    for(const move of stay.internal_moves||[])html+=modeStrip({source:move.source||stay.source,target:'trip',started_at:move.start,ended_at:move.end},TRIP_MODES,move.mode,move.mode_corrected,null)
      .replace('<div class="mode-options">',`<div class="mode-caption">Moved within · ${safe(distanceText(move.distance_km))} · ${safe(clock(move.start))}</div><div class="mode-options">`);
  }
  if(!html)return;
  row.insertAdjacentHTML('afterend',html);
  document.querySelectorAll('#day-timeline .tl-mode-picker [data-set-mode]').forEach(button=>button.onclick=async()=>{
    const subject=JSON.parse(button.closest('.tl-mode-picker').dataset.modeSubject);
    try{
      await post('/api/mode-overrides',{...subject,mode:button.dataset.setMode});
      toast(button.dataset.setMode==='auto'?'Automatic mode restored':`Set to ${modeInfo[button.dataset.setMode].noun.toLowerCase()}`);
      await refreshExplore(false);
    }catch(error){toast(error.message)}
  });
}
function drawSelection(zoom){
  highlightLayer?.clearLayers();
  const item=selectedItem();
  const key=state.selected?`${state.selected.type}:${state.selected.key}`:null;
  document.querySelectorAll('#day-timeline [data-item]').forEach(row=>{const active=row.dataset.item===key;row.classList.toggle('selected',active);row.setAttribute('aria-pressed',String(active))});
  renderModePicker();
  $('fit-day').hidden=!item;
  if(!item){if(state.day)$('map-note').textContent=mapNote(state.day);return}
  const subject=item.type==='trip'?item.trip:item.stay;
  const color=item.type==='trip'?modeInfo[item.trip.mode]?.color||'#176d69':stayColor(item.stay);
  const tripNote=trip=>trip.loop?`Round trip from ${trip.from_name||'stay'}${trip.turnaround?.name?' via '+trip.turnaround.name:''}`
    :`${modeInfo[trip.mode]?.label||'Trip'}: ${trip.from_name||'Stay'} → ${trip.to_name||'Stay'}`;
  $('map-note').textContent=item.type==='trip'?tripNote(item.trip):(item.stay.display_name||'Stay');
  if(!map)return;
  const lines=[...(subject.route_segments||[]),...(subject.internal_moves||[]).flatMap(move=>move.route_segments||[])].map(mapPath);
  for(const line of lines){L.polyline(line,{color:'#fff',weight:10,opacity:.95,interactive:false}).addTo(highlightLayer);L.polyline(line,{color,weight:6,opacity:1,interactive:false}).addTo(highlightLayer)}
  if(item.type==='stay')L.circleMarker([item.stay.lat,item.stay.lon],{radius:item.stay.kind==='area'?6:10,color:'#fff',weight:3,fillColor:color,fillOpacity:1})
    .bindPopup(pointPopup(item.stay.display_name||'Stay',[stayWhen(item.stay,state.day)+' · '+durationText(item.stay.observed_minutes)],item.stay.lat,item.stay.lon,editStayAction(item.stay.id))).addTo(highlightLayer);
  const turn=item.type==='trip'&&item.trip.loop?item.trip.turnaround:null;
  if(turn)L.circleMarker([turn.lat,turn.lon],{radius:8,color:'#fff',weight:3,fillColor:color,fillOpacity:1})
    .bindPopup(pointPopup(turn.stop_minutes?(turn.name||'Stop'):'Farthest point',[turn.stop_minutes?`Stopped ${durationText(turn.stop_minutes)} · ${clock(turn.arrived_at)}`:clock(turn.arrived_at)],turn.lat,turn.lon)).addTo(highlightLayer);
  if(!zoom)return;
  if(item.type==='stay'&&item.stay.kind==='area'&&!item.stay.internal_moves?.length)map.fitBounds(L.latLng(item.stay.lat,item.stay.lon).toBounds((item.stay.radius_m||100)*2.4),{maxZoom:16});
  else if(lines.flat().length>1)map.fitBounds(L.latLngBounds(lines.flat()).pad(.2),{maxZoom:16});
  else map.setView([subject.lat??subject.from_lat,subject.lon??subject.from_lon],16);
}
function selectItem(type,key){
  const same=state.selected?.type===type&&state.selected?.key===key;
  state.selected=same?null:{type,key};
  state.focusPlace=null;
  drawMap(false);
  if(same)fitDay();else drawSelection(true);
  revealMap();
}
function mapNote(day){return day.route_segments.length?day.route_segments.length+' recorded path segment'+(day.route_segments.length===1?'':'s'):day.points?day.timeline_points.toLocaleString()+' key fixes':state.focusPlace?'Location: '+(state.focusPlace.name||state.focusPlace.display_name||'selected fix'):'Recorded route'}
function renderChart(){const points=[...state.timeline].reverse().filter(p=>p.speed_mps!=null).slice(-100);const box=$('speed-chart');if(points.length<2){box.innerHTML='<span class="empty">More timed samples are needed for a speed chart.</span>';return}const top=Math.max(10,...points.map(p=>p.speed_mps*3.6));const poly=points.map((p,i)=>`${(i/(points.length-1)*600).toFixed(1)},${(110-p.speed_mps*3.6/top*94).toFixed(1)}`).join(' ');box.innerHTML=`<svg viewBox="0 0 600 125" preserveAspectRatio="none" aria-label="Speed chart"><line x1="0" y1="110" x2="600" y2="110" stroke="#dce6eb"/><line x1="0" y1="63" x2="600" y2="63" stroke="#edf2f5"/><polyline fill="none" stroke="#177c91" stroke-width="3" points="${poly}"/><text x="0" y="12" fill="#98a7b4" font-size="10">${top.toFixed(0)} km/h</text><text x="0" y="123" fill="#98a7b4" font-size="10">${safe(formatTime(points[0].timestamp))}</text></svg>`}
function renderSamples(){
  const rows=state.sampleView==='timeline'?state.timeline:state.points;
  const start=state.rawPage*state.rawPageSize;
  $('sample-count').textContent=state.sampleView==='timeline'
    ?`${state.timeline.length.toLocaleString()} informational fixes from ${state.rawTotal.toLocaleString()} raw samples. Stationary periods keep their edges.`
    :rows.length?`Showing ${(start+1).toLocaleString()}–${(start+rows.length).toLocaleString()} of ${state.rawTotal.toLocaleString()} raw samples.`:`No raw samples on this page.`;
  const visible=state.sampleView==='timeline'?rows.slice(0,100):rows;
  $('points-table').innerHTML=visible.map((p,index)=>'<tr><td>'+safe(formatTime(p.timestamp))+'</td><td>'+safe(state.sampleView==='timeline'?roleName(p.role):'Raw sample')+'</td><td>'+safe(p.source)+'</td><td><span class="mode-tag '+safe(p.mode)+'">'+safe(modeName(p.mode))+'</span></td><td>'+safe(kmh(p))+(p.speed_origin==='estimated'?' <small>est.</small>':p.speed_origin==='reported_adjusted'?' <small>adjusted</small>':'')+'</td><td>'+p.lat.toFixed(5)+', '+p.lon.toFixed(5)+'</td><td>'+(p.accuracy_m==null?'—':Math.round(p.accuracy_m)+' m')+'</td><td><button class="table-map" data-sample-map="'+index+'">Map</button></td></tr>').join('')||'<tr><td colspan="8" class="empty">No fixes on this day.</td></tr>';
  $('points-table').querySelectorAll('[data-sample-map]').forEach(button=>button.onclick=()=>focusPlaceOnMap({...visible[+button.dataset.sampleMap],display_name:formatTime(visible[+button.dataset.sampleMap].timestamp)}));
  $('raw-pagination').hidden=state.sampleView!=='raw'||state.rawTotal<=state.rawPageSize;
  $('raw-page').textContent=`Page ${state.rawPage+1} of ${Math.max(1,Math.ceil(state.rawTotal/state.rawPageSize))}`;
  $('raw-previous').disabled=state.rawPage===0;
  $('raw-next').disabled=start+rows.length>=state.rawTotal;
}
function stayWhen(stay,day){
  const early=new Date(stay.started_at)<new Date(day.start),late=new Date(stay.ended_at)>new Date(day.end);
  if(early&&stay.ongoing)return'Since yesterday';
  if(early&&late)return'All day';
  if(early)return`Until ${clock(stay.ended_at)}`;
  if(late)return`From ${clock(stay.started_at)}`;
  if(stay.ongoing)return`Since ${clock(stay.started_at)}`;
  return`${clock(stay.started_at)} – ${clock(stay.ended_at)}`;
}
function pinBadge(number){return number?`<span class="pin-badge">${number}</span>`:`<span class="pin-badge home" title="Home">${placeIcons.home}</span>`}
function activityTag(stay){const text=stayActivityText(stay);return text?`<span class="activity-tag" style="--mode:${stayColor(stay)}">${stay.activity?modeIcon(stay.activity):''}${safe(text)}</span>`:''}
function roundTripNote(trip){
  // Say where a round trip went, so Home -> Home reads as an errand rather than a repeat.
  const turn=trip.turnaround,left=trip.from_name?`Left ${trip.from_name}`:'Left';
  if(turn?.stop_minutes)return `${left} · stopped ${durationText(turn.stop_minutes)} ${turn.name?'near '+turn.name:distanceText(turn.distance_m/1000)+' away'}`;
  if(turn)return `${left} · went ${distanceText(turn.distance_m/1000)} out and came back`;
  return `${left} and came back`;
}
function movedWithin(stay){
  // Walking between buildings or moving the car belongs to the visit, not to a trip "to" where you already were.
  const moves=(stay.internal_moves||[]).map(move=>`${modeInfo[move.mode]?.label||'Move'} ${distanceText(move.distance_km)}`);
  return moves.length?`<span class="tl-within">Moved within · ${safe(moves.join(' · '))}</span>`:'';
}
function renderTimeline(day){
  const numbers=new Map(day.visited_places.map((place,index)=>[place.cluster_id,index+1]));
  const items=[...day.stays.map(stay=>({type:'stay',at:stay.started_at,stay})),...day.trips.map(trip=>({type:'trip',at:trip.start,trip})),
    ...(day.coverage_gaps||[]).map(gap=>({type:'gap',at:gap.start,gap}))]
    .sort((a,b)=>new Date(a.at)-new Date(b.at)||(a.type==='stay'?-1:1));
  const counts=[`${day.stays.length} stay${day.stays.length===1?'':'s'}`,`${day.trips.length} trip${day.trips.length===1?'':'s'}`];
  if((day.timeline_sources||[]).length>1)counts.push(`${day.timeline_sources.length} sources`);
  $('timeline-count').textContent=day.stays.length||day.trips.length?counts.join(' · '):'';
  $('timeline-count').title=(day.timeline_sources||[]).length>1?`Combined from ${day.timeline_sources.join(', ')}; where sources overlap, the one with the most samples is shown.`:'';
  $('day-timeline').innerHTML=items.map(item=>{
    if(item.type==='gap')return `<li class="tl-gap"><span>No location data</span><small>${safe(clock(item.gap.start))} – ${safe(clock(item.gap.end))} · ${safe(durationText(item.gap.minutes))}</small></li>`;
    if(item.type==='stay'){
      const stay=item.stay;
      return `<li><button type="button" class="tl-stay" data-item="stay:${stay.id}" aria-pressed="false">${pinBadge(numbers.get(stay.cluster_id))}<span class="tl-body"><strong>${safe(stay.display_name||stay.place_name||'Observed stay')}</strong><small>${safe(stayWhen(stay,day))} · ${safe(durationText(stay.observed_minutes))}</small>${activityTag(stay)}${movedWithin(stay)}</span></button></li>`;
    }
    const trip=item.trip,info=modeInfo[trip.mode]||{label:'Trip',color:'#6f7b76'};
    const notes=[`${clock(trip.start)} – ${clock(trip.end)}`,durationText(trip.duration_minutes)];
    if(!trip.coverage_complete)notes.push('coverage gap');
    if(trip.mode_corrected)notes.push('set by you');
    // A trip that ends where it began left and came back; say so rather than implying travel to where you already were.
    const title=trip.loop?`Round trip · ${info.label} ${distanceText(trip.distance_km)}`:`${info.label} · ${distanceText(trip.distance_km)}`;
    const from=trip.loop?`<span class="tl-within">${safe(roundTripNote(trip))}</span>`:'';
    return `<li><button type="button" class="tl-trip" data-item="trip:${tripKey(trip)}" aria-pressed="false" style="--mode:${info.color}">${modeIcon(trip.mode)}<span class="tl-body"><strong>${safe(title)}</strong><small>${safe(notes.join(' · '))}</small>${from}</span></button></li>`;
  }).join('')||'<li class="empty">No stays yet on this day. A stay needs 15 minutes of accurate samples in one place or area.</li>';
  $('day-timeline').querySelectorAll('[data-item]').forEach(button=>button.onclick=()=>{const [type,...rest]=button.dataset.item.split(':');const key=rest.join(':');selectItem(type,type==='stay'?+key:key)});
}
function renderDayPlaces(day){
  const numbers=new Map(day.visited_places.map((place,index)=>[place.cluster_id,index+1]));
  $('visited-places').innerHTML=day.places_including_home.map(place=>{
    const activities=Object.entries(place.activities||{}).map(([mode,count])=>modeName(mode)+(count>1?` ×${count}`:'')).join(', ');
    const details=[`First seen ${clock(place.first_seen)}`,`${durationText(place.observed_minutes)} observed`];
    if(place.visits>1)details.push(`${place.visits} visits`);
    if(activities)details.push(activities);
    return `<div class="list-item"><div class="place-main">${pinBadge(numbers.get(place.cluster_id))}<div><strong>${safe(place.name||place.display_name)}</strong><small>${safe(details.join(' · '))}${place.address&&place.address!==place.name?'<br>'+safe(place.address):''}</small></div></div><div class="actions"><button data-day-place="${place.cluster_id}">Map</button><button data-edit-day-place="${place.cluster_id}">Edit</button></div></div>`;
  }).join('')||'<p class="empty">No confirmed stays on this day. Sparse fixes may miss a visit.</p>';
  $('visited-places').querySelectorAll('[data-day-place]').forEach(button=>button.onclick=()=>{const place=day.places_including_home.find(item=>item.cluster_id===+button.dataset.dayPlace);focusPlaceOnMap({...place,display_name:place.name||place.address||'Visited place'})});
  $('visited-places').querySelectorAll('[data-edit-day-place]').forEach(button=>button.onclick=()=>editVisitedPlace(day.places_including_home.find(item=>item.cluster_id===+button.dataset.editDayPlace)));
}
function renderSummary(day){
  $('stat-distance').textContent=distanceText(day.movement_km??day.stats.distance_km);
  $('stat-moving').textContent=durationText(day.movement_minutes);
  $('stat-places').textContent=day.visited_places.length.toLocaleString();
  $('stat-trips').textContent=day.trips.length.toLocaleString();
  $('activity-chips').innerHTML=Object.entries(day.activity_totals).filter(([,totals])=>totals.count).map(([mode,totals])=>
    `<span class="chip" style="--mode:${modeInfo[mode].color}">${modeIcon(mode)}<strong>${modeInfo[mode].noun}</strong><span>${distanceText(totals.distance_km)} · ${durationText(totals.minutes)}</span></span>`).join('');
  const modes=new Set([...day.trips.map(trip=>trip.mode),...day.stays.flatMap(stay=>[stay.activity,...(stay.internal_moves||[]).map(move=>move.mode)]).filter(Boolean)]);
  $('map-legend').innerHTML='<span><i class="legend-line" style="--mode:#8ea8a2"></i>Recorded path</span>'+[...modes].filter(mode=>modeInfo[mode]).map(mode=>`<span><i class="legend-line" style="--mode:${modeInfo[mode].color}"></i>${modeInfo[mode].noun}</span>`).join('');
}
function renderExplore(day,coverage){
  const hasHistory=day.points>0;
  const hasArchive=coverage.sources.some(source=>source.points>0);
  const hasMapContent=hasHistory||!!state.focusPlace||!!state.current;
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
  $('locate').hidden=!(state.current||day.last_location);
  $('history-layout').classList.toggle('no-data',!hasMapContent);
  $('history-layout').classList.toggle('map-only',!hasHistory);
  $('day-context').hidden=!hasHistory;
  if(hasHistory)$('day-context').textContent=(day.points===1?'One fix at '+clock(day.first_fix):'Fixes from '+clock(day.first_fix)+' to '+clock(day.last_fix))+' · '+day.points.toLocaleString()+' samples · gaps are unobserved';
  // Keep the map where the viewer left it on background refreshes; fit only for a new day or source.
  const viewKey=`${day.date}|${day.source||''}`;
  const newView=state.fitKey!==viewKey;
  if(newView){state.fitKey=viewKey;state.selected=null}
  let zoomToSelection=false;
  if(state.pendingStayId!=null){state.selected={type:'stay',key:state.pendingStayId};state.pendingStayId=null;zoomToSelection=true}
  if(!selectedItem())state.selected=null;
  renderSummary(day);
  renderTimeline(day);
  renderDayPlaces(day);
  $('map-detail-section').hidden=true;
  renderSamples();
  renderChart();
  const redraw=()=>{drawMap(newView&&!zoomToSelection);if(zoomToSelection){drawSelection(true);document.querySelector('#day-timeline .selected')?.scrollIntoView({block:'nearest'})}};
  if(map&&(newView||zoomToSelection))setTimeout(()=>{map.invalidateSize();redraw()},20);else redraw();
}
function renderCurrent(){
  const point=state.current;
  $('current-location').hidden=!point;
  if(!point)return;
  $('current-place').innerHTML=placeIcon({...point,name:point.place_name})+'<span>'+safe(point.place_name||point.address||'Unknown place')+'</span>';
  const age=point.age_seconds<60?'just now':point.age_seconds<3600?`${Math.floor(point.age_seconds/60)} min ago`:`${Math.floor(point.age_seconds/3600)} hr ago`;
  $('current-updated').textContent=`Updated ${formatTime(point.timestamp)} · ${age}${point.stale?' · stale':''}`;
  $('current-coordinates').textContent=`${Number(point.lat).toFixed(6)}, ${Number(point.lon).toFixed(6)}`;
}
function fillPlaceSelectors(){const select=$('reminder-place'),value=select.value;select.innerHTML='<option value="">Select a saved place</option>'+state.places.map(p=>`<option value="${p.id}">${safe(p.name)}</option>`).join('')+'<option value="other">Other place or coordinates…</option><option value="named">Any place named…</option>';select.value=[...select.options].some(option=>option.value===value)?value:'';toggleReminderDestination()}
function toggleReminderDestination(){const choice=$('reminder-place').value,custom=choice==='other',named=choice==='named';$('reminder-custom-place').hidden=!custom;$('reminder-named-place').hidden=!named;$('reminder-match-name').required=named;for(const id of ['reminder-lat','reminder-lon'])$(id).required=custom}
function reminderWhere(reminder){return reminder.match_name?`Any place named “${reminder.match_name}”`:reminder.place_name}
function setReminderDestination(place){$('reminder-place-name').value=(place.name||place.place_name||'').slice(0,120);$('reminder-lat').value=place.lat??'';$('reminder-lon').value=place.lon??'';$('reminder-address').value=place.address||'';$('reminder-category').value=place.category||'';$('reminder-provider-id').value=place.provider_place_id||'';$('reminder-search-results').innerHTML=''}
function openPlaceDialog(place={}){
  $('place-form').reset();$('search-results').innerHTML='';$('search').value='';$('place-id').value=place.id||'';$('place-category').value=place.category||'';$('place-provider-id').value=place.provider_place_id||'';$('place-name').value=place.name||'';$('place-lat').value=place.lat??'';$('place-lon').value=place.lon??'';$('place-radius').value=place.radius_m||150;$('place-address').value=place.address||'';$('place-dialog-title').textContent=place.id?'Edit place':'Add place';$('place-dialog').showModal();
}
function metresBetween(aLat,aLon,bLat,bLon){
  const rad=Math.PI/180,dLat=(bLat-aLat)*rad,dLon=(bLon-aLon)*rad;
  const h=Math.sin(dLat/2)**2+Math.cos(aLat*rad)*Math.cos(bLat*rad)*Math.sin(dLon/2)**2;
  return 12742000*Math.asin(Math.min(1,Math.sqrt(h)));
}
function labelTarget(place,visits){
  // Cover the visits being corrected (or the place's observed extent) with a margin.
  const center=visits.length===1?visits[0]:place;
  const reach=visits.length?Math.max(...visits.map(stay=>metresBetween(center.lat,center.lon,stay.lat,stay.lon)+(stay.radius_m||0)))
    :(place.extent_m||place.radius_m||0);
  return {lat:+Number(center.lat).toFixed(6),lon:+Number(center.lon).toFixed(6),radius_m:Math.round(Math.min(1000,Math.max(50,reach?reach+25:50)))};
}
function visitTimes(visits){return visits.map(stay=>`${clock(stay.started_at)} – ${clock(stay.ended_at)}`).join(', ')}
function editVisitedPlace(place,visits=null){
  // visits: the stays this fix is about. A pin or day row covers the place's visits that day;
  // the Places page has no particular visit, so only the every-visit choice applies there.
  if(!place)return;
  visits=visits??(state.day&&place.cluster_id!=null?state.day.stays.filter(stay=>stay.cluster_id===place.cluster_id):[]);
  const target=labelTarget(place,visits);
  const labelIds=[...new Set([place.label_id,...visits.map(stay=>stay.label_id)].filter(id=>id!=null))];
  const current=(state.labels||[]).find(label=>label.id===labelIds[0]);
  state.labelEdit={...target,visits,labelIds,editing:labelIds.length===1?labelIds[0]:null};
  $('label-form').reset();
  $('label-name').value=place.name||place.display_name||'';
  $('label-category').value=place.category||'';$('label-provider-id').value=place.provider_place_id||'';$('label-address').value=place.address||'';
  $('scope-visit-option').hidden=!visits.length;
  if(visits.length){
    const [year,month,day]=(state.day?.date||'').split('-').map(Number);
    const dayName=state.day?new Date(year,month-1,day).toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'}):'';
    $('scope-visit-title').textContent=visits.length===1?'Only this visit':`Only the visits on ${dayName}`;
    $('scope-visit-detail').textContent=visitTimes(visits)+(visits.length===1?'':` (${visits.length} visits)`);
  }
  $('scope-spot-detail').textContent=`Past and future visits within ${target.radius_m} m`;
  const scope=!visits.length||(current&&!current.started_at)?'spot':'visit';
  document.querySelector(`input[name="label-scope"][value="${scope}"]`).checked=true;
  $('label-where').textContent=`Pinned at ${coordinateText(target.lat,target.lon)}.`;
  $('reset-label').hidden=!labelIds.length;
  loadNearbyPlaces(target);
  $('label-dialog').showModal();
}
async function loadNearbyPlaces(target){
  const box=$('nearby-places'),request=++loadNearbyPlaces.request;
  box.innerHTML='<p class="empty">Looking for places around this visit…</p>';
  $('nearby-note').textContent='';
  try{
    const radius=Math.max(500,target.radius_m+50);
    const places=await api('/api/nearby-places?'+new URLSearchParams({lat:target.lat,lon:target.lon,radius_m:radius,limit:15}));
    if(request!==loadNearbyPlaces.request)return;
    $('nearby-note').textContent=places.length?`${places.length} within ${radius>=1000?(radius/1000).toFixed(1)+' km':radius+' m'}`:'';
    box.innerHTML=places.map((place,index)=>`<div class="result-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.name)}</strong><span>${safe([place.distance_m!=null?place.distance_m+' m away':'',place.address&&place.address!==place.name?place.address:''].filter(Boolean).join(' · '))}</span></div></div><button type="button" data-nearby="${index}">Use</button></div>`).join('')||'<p class="empty">No named places nearby. Type the name below.</p>';
    box.querySelectorAll('[data-nearby]').forEach(button=>button.onclick=()=>{
      const place=places[+button.dataset.nearby];
      $('label-name').value=place.name.slice(0,120);$('label-category').value=place.category||'';$('label-provider-id').value=place.provider_place_id||'';$('label-address').value=place.address||'';
      box.querySelectorAll('.result-item').forEach(item=>item.classList.toggle('chosen',item===button.closest('.result-item')));
      $('label-name').focus();
    });
  }catch(error){if(request===loadNearbyPlaces.request)box.innerHTML=`<p class="empty">${safe(error.message)}</p>`}
}
loadNearbyPlaces.request=0;
async function afterPlaceNamesChanged(message){
  await refreshMeta();await Promise.all([refreshExplore(false),refreshInsights()]);toast(message);
}
function revealMap(){
  // Bring the map on screen only when it is not already fully visible, so a list below keeps its place.
  const shell=document.querySelector('.map-shell'),box=shell.getBoundingClientRect();
  if(box.top<0||box.bottom>window.innerHeight)shell.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function focusPlaceOnMap(place){
  state.focusPlace=place;state.selected=null;
  const wasOpen=$('explore-view').classList.contains('active')&&!$('history-layout').classList.contains('no-data');
  showView('explore');
  // Re-render only when the map was hidden; otherwise the lists keep their scroll position.
  if(!wasOpen&&state.day&&state.coverage)renderExplore(state.day,state.coverage);else{drawMap(false);$('map-note').textContent='Location: '+(place.name||place.display_name||'selected point')}
  setTimeout(()=>{map?.invalidateSize();map?.setView([place.lat,place.lon],16);revealMap()},30);
}
function renderPlaces(){
  fillPlaceSelectors();
  // Saved-place totals include Home, which the visited list leaves out.
  const usage=new Map(Object.entries(state.savedStats||{}).map(([id,stats])=>[+id,stats]));
  const saved=[...state.places].sort((a,b)=>(usage.get(b.id)?.observed_minutes||0)-(usage.get(a.id)?.observed_minutes||0)||a.name.localeCompare(b.name));
  $('places-list').innerHTML=saved.map(p=>{const seen=usage.get(p.id);const activity=seen?`${seen.visits} visit${seen.visits===1?'':'s'} · ${seen.observed_minutes.toFixed(0)} min observed`:'No confirmed visits yet';return `<div class="list-item"><div class="place-main">${placeIcon(p)}<div><strong>${safe(p.name)}</strong><small>${safe(activity)} · ${Math.round(p.radius_m)} m radius${p.address?' · '+safe(p.address):''}</small></div></div><div class="actions"><button data-focus="${p.id}">Map</button><button data-edit="${p.id}">Edit</button><button data-delete-place="${p.id}" class="danger-button">Delete</button></div></div>`}).join('')||'<div class="empty">No saved places yet.</div>';
  $('places-list').querySelectorAll('[data-focus]').forEach(button=>button.onclick=()=>focusPlaceOnMap(state.places.find(place=>place.id===+button.dataset.focus)));
  $('places-list').querySelectorAll('[data-edit]').forEach(button=>button.onclick=()=>openPlaceDialog(state.places.find(place=>place.id===+button.dataset.edit)));
  $('places-list').querySelectorAll('[data-delete-place]').forEach(button=>button.onclick=async()=>{
    const place=state.places.find(item=>item.id===+button.dataset.deletePlace);
    if(!place||!confirm(`Delete “${place.name}”? Visits there will be named automatically again. Reminders that use it keep working.`))return;
    try{await post('/api/places/delete',{id:place.id});await afterPlaceNamesChanged('Place deleted')}catch(error){toast(error.message)}
  });
  $('all-visited-places').innerHTML=state.visitedPlaces.map((place,index)=>`<div class="list-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.display_name)}</strong><small>${place.visits} visit${place.visits===1?'':'s'} · ${place.observed_minutes.toFixed(0)} min observed · Last ${safe(formatTime(place.last_seen))}</small></div></div><div class="actions"><button data-map-visited="${index}">Map</button><button data-edit-visited="${index}">Edit place</button></div></div>`).join('')||'<div class="empty">No confirmed visited places yet.</div>';
  $('all-visited-places').querySelectorAll('[data-map-visited]').forEach(button=>button.onclick=()=>focusPlaceOnMap(state.visitedPlaces[+button.dataset.mapVisited]));
  $('all-visited-places').querySelectorAll('[data-edit-visited]').forEach(button=>button.onclick=()=>editVisitedPlace(state.visitedPlaces[+button.dataset.editVisited]));
}
function renderReminders(){
  $('reminders-list').innerHTML=state.reminders.map(r=>`<div class="list-item"><div class="place-main">${placeIcon(r)}<div><strong>${safe(r.text)}</strong><small>${safe(r.match_name?reminderWhere(r):'At '+r.place_name)} · Created ${safe(formatTime(r.created_at))}</small></div></div><button data-cancel-reminder="${r.id}">Cancel</button></div>`).join('')||'<div class="empty">No active reminders.</div>';
  $('reminders-list').querySelectorAll('[data-cancel-reminder]').forEach(button=>button.onclick=async()=>{try{await post('/api/reminders/cancel',{id:+button.dataset.cancelReminder});await refreshMeta();toast('Reminder cancelled')}catch(error){toast(error.message)}});
  $('reminder-history').innerHTML=state.reminderHistory.map(item=>`<div class="list-item"><div class="place-main">${placeIcon(item)}<div><strong>${safe(item.text)}</strong><small>${safe(reminderWhere(item))} · ${item.status==='triggered'?'Triggered '+safe(formatTime(item.triggered_at)):'Cancelled '+safe(formatTime(item.cancelled_at||item.created_at))}</small></div></div></div>`).join('')||'<div class="empty">Triggered and cancelled reminders will appear here.</div>';
}
function renderInsights(){const data=state.insights;if(!data)return;const prediction=data.prediction;$('prediction').textContent=prediction.prediction?`Likely next from ${prediction.from}: ${prediction.prediction} (${Math.round(prediction.frequency*100)}% of ${prediction.observations} observed departures).${prediction.current_fix_stale?' Latest location is stale; verify before acting.':''}`:`Next place: insufficient evidence (${prediction.reason}).`;$('pattern-places').innerHTML=data.frequent_places.slice(0,8).map((place,index)=>`<div class="list-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.display_name)}</strong><small>${place.visits} visit${place.visits===1?'':'s'} · ${place.observed_minutes.toFixed(0)} min observed</small></div></div><button data-pattern-place="${index}">Map</button></div>`).join('')||'<div class="empty">Common places appear after confirmed stays.</div>';$('pattern-places').querySelectorAll('[data-pattern-place]').forEach(button=>button.onclick=()=>focusPlaceOnMap(data.frequent_places[+button.dataset.patternPlace]));$('transitions').innerHTML=data.transitions.slice(0,8).map((t,index)=>`<div class="list-item"><div><strong>${safe(t.from_name)} → ${safe(t.to_name)}</strong><small>${t.count} observed transition${t.count===1?'':'s'}</small></div><button data-transition="${index}">Map arrival</button></div>`).join('')||'<div class="empty">Transitions appear after visits to two or more places.</div>';$('transitions').querySelectorAll('[data-transition]').forEach(button=>button.onclick=()=>{const transition=data.transitions[+button.dataset.transition];focusPlaceOnMap({lat:transition.to_lat,lon:transition.to_lon,display_name:transition.to_name})})}
async function refreshCoverage(){const data=await api('/api/coverage');$('coverage').innerHTML=data.sources.map(s=>`<div class="list-item"><div><strong>${safe(s.source)} · ${s.points.toLocaleString()} fixes</strong><small>${safe(formatTime(s.first))} → ${safe(formatTime(s.last))}</small></div></div>`).join('')+(data.gaps_over_24h.length?`<p class="muted">${data.gaps_over_24h.length} gaps over 24 hours. Missing coverage cannot establish where you were.</p>`:'')||'<div class="empty">No source data yet.</div>'}
async function refreshInsights(){state.insights=await api('/api/insights?limit=50');renderInsights()}
async function refreshMeta(){let overview;[state.places,overview,state.reminders,state.reminderHistory,state.events,state.labels]=await Promise.all([api('/api/places'),api('/api/place-overview?limit=100'),api('/api/reminders'),api('/api/reminder-history?limit=200'),api('/api/events'),api('/api/place-labels')]);state.visitedPlaces=overview.visited;state.savedStats=overview.saved;renderPlaces();renderReminders();drawMap()}
async function refreshRawPage(){
  const query=params();
  query.set('limit',state.rawPageSize);
  query.set('offset',state.rawPage*state.rawPageSize);
  state.points=await api('/api/observations?'+query);
  renderSamples();
}
async function refreshExplore(resetRawPage=true){
  const request=++exploreRequest;
  if(resetRawPage)state.rawPage=0;
  const date=$('day-date').value;
  const source=$('source').value;
  const timezone=state.settings?.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
  const dayQuery=new URLSearchParams({date,timezone});
  if(source)dayQuery.set('source',source);
  const query=params();
  const rawQuery=new URLSearchParams(query);
  rawQuery.set('limit',state.rawPageSize);
  rawQuery.set('offset',state.rawPage*state.rawPageSize);
  // Background refreshes stay quiet; a day or source change dims the page while it loads.
  if(resetRawPage)$('explore-view').classList.add('loading');
  let day,timeline,points,coverage;
  try{
    [day,timeline,points,coverage]=await Promise.all([
      api('/api/day?'+dayQuery),api('/api/timeline?'+query),api('/api/observations?'+rawQuery),api('/api/coverage')
    ]);
  }finally{
    if(request===exploreRequest)$('explore-view').classList.remove('loading');
  }
  if(request!==exploreRequest)return;
  const selected=$('source').value;
  $('source').innerHTML='<option value="">All sources</option>'+coverage.sources.map(item=>'<option value="'+safe(item.source)+'">'+safe(item.source)+'</option>').join('');
  $('source').value=selected;
  state.day=day;
  state.timeline=timeline;
  state.points=points;
  state.rawTotal=day.points;
  state.coverage=coverage;
  // A past day is bookmarkable as #date=YYYY-MM-DD; today keeps a clean URL.
  const hash=day.date===localDateKey()?'':'#date='+day.date;
  if(location.hash!==hash)history.replaceState(null,'',location.pathname+location.search+hash);
  renderExplore(day,coverage);
}
function renderDailySummaries(){$('daily-summaries').innerHTML=state.dailySummaries.slice(0,10).map(item=>`<div class="list-item"><div><strong>${safe(item.date)}</strong><small>${item.trip_count} trips · ${Number(item.distance_km).toFixed(1)} km · ${item.place_count} places · ${item.reminder_count} reminders</small></div><button type="button" data-open-summary="${safe(item.date)}">View</button></div>`).join('')||'<div class="empty">A summary will be saved after the first completed day.</div>';
  $('daily-summaries').querySelectorAll('[data-open-summary]').forEach(button=>button.onclick=()=>{showView('summaries');openSummary(button.dataset.openSummary)})}
function longDate(value){const [year,month,day]=value.split('-').map(Number);return new Date(year,month-1,day).toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric',year:'numeric'})}
function summaryLine(item){return `${item.trip_count} trip${item.trip_count===1?'':'s'} · ${Number(item.distance_km).toFixed(1)} km · ${item.place_count} place${item.place_count===1?'':'s'}${item.reminder_count?` · ${item.reminder_count} reminder${item.reminder_count===1?'':'s'}`:''}`}
async function refreshSummaries(){
  state.summaryCatalog=await api('/api/daily-summaries?limit=1000');
  $('summary-count').textContent=state.summaryCatalog.length?`${state.summaryCatalog.length} day${state.summaryCatalog.length===1?'':'s'}`:'';
  $('summary-index').innerHTML=state.summaryCatalog.map(item=>`<button type="button" class="summary-day${item.date===state.summaryDate?' selected':''}" data-summary="${safe(item.date)}"><strong>${safe(longDate(item.date))}</strong><small>${safe(summaryLine(item))}</small></button>`).join('')
    ||'<p class="empty">Nothing stored yet. The first record is written after midnight, or open a day above to build one.</p>';
  $('summary-index').querySelectorAll('[data-summary]').forEach(button=>button.onclick=()=>openSummary(button.dataset.summary));
  if(!state.summaryDate&&state.summaryCatalog[0])openSummary(state.summaryCatalog[0].date);
}
async function openSummary(date,build=false){
  state.summaryDate=date;$('summary-date').value=date;
  document.querySelectorAll('#summary-index [data-summary]').forEach(button=>button.classList.toggle('selected',button.dataset.summary===date));
  const box=$('summary-detail');
  box.innerHTML=`<p class="empty">${build?'Building':'Loading'} the summary for ${safe(longDate(date))}…</p>`;
  try{
    // Building writes the record (or refreshes it if its data changed); reading shows exactly what is stored.
    if(build){await api('/api/daily-summary?'+new URLSearchParams({date}));await refreshSummaries()}
    renderSummaryDetail(await api('/api/daily-summary/stored?'+new URLSearchParams({date})));
  }catch(error){
    if(state.summaryDate!==date)return;
    box.innerHTML=`<div class="summary-missing"><h2>${safe(longDate(date))}</h2><p class="empty">${safe(error.message)}.</p><button type="button" class="primary-button" id="build-summary">Build and store it</button></div>`;
    $('build-summary').onclick=()=>openSummary(date,true);
  }
}
function summarySection(title,rows,empty){return `<section class="summary-block"><h3>${safe(title)}</h3>${rows.length?`<div class="list">${rows.join('')}</div>`:`<p class="empty">${safe(empty)}</p>`}</section>`}
function summaryRow(title,detail){return `<div class="list-item"><div><strong>${safe(title)}</strong>${detail?`<small>${safe(detail)}</small>`:''}</div></div>`}
function renderSummaryDetail(record){
  if(state.summaryDate!==record.date)return;
  const value=record.summary,totals=value.totals||{};
  const facts=[['Trip distance',`${Number(totals.distance_km||0).toFixed(1)} km`],['Sampled distance',`${Number(totals.sampled_distance_km||0).toFixed(1)} km`],
    ['Moving',durationText((totals.moving_hours||0)*60)],['Trips',totals.trips??0],['Places',totals.places??0],
    ['Fixes',`${(totals.timeline_fixes??0).toLocaleString()} key of ${(totals.raw_fixes??0).toLocaleString()}`],
    ['Reminders',`${totals.reminders_triggered??0} triggered · ${totals.reminders_created??0} created`]];
  const activity=Object.entries(value.activity_totals||{}).filter(([mode,item])=>item.count&&modeInfo[mode]).map(([mode,item])=>
    `<span class="chip" style="--mode:${modeInfo[mode].color}">${modeIcon(mode)}<strong>${modeInfo[mode].noun}</strong><span>${distanceText(item.distance_km)} · ${durationText(item.minutes)}</span></span>`).join('');
  const visits=(value.visits||[]).map(visit=>summaryRow(visit.place||'Unnamed place',[`${clock(visit.arrival)} – ${clock(visit.departure)}`,durationText(visit.observed_minutes),
    visit.activity?`${modeName(visit.activity)}${visit.distance_km?' '+distanceText(visit.distance_km):''}`:'',(visit.moved_within||[]).length?'moved within: '+visit.moved_within.map(move=>`${modeName(move.mode).toLowerCase()} ${distanceText(move.distance_km)}`).join(', '):'',visit.ongoing?'ongoing':''].filter(Boolean).join(' · ')));
  const trips=(value.trips||[]).slice().sort((a,b)=>a.start.localeCompare(b.start)).map(trip=>summaryRow(`${trip.round_trip?'Round trip from '+(trip.from||'a stay'):(trip.from||'A stay')+' → '+(trip.to||'a stay')}`,
    [`${modeName(trip.mode)} ${distanceText(trip.distance_km)}`,`${clock(trip.start)} – ${clock(trip.end)}`,durationText(trip.duration_minutes),trip.coverage_complete?'':'coverage gap'].filter(Boolean).join(' · ')));
  const places=(value.places||[]).map(place=>summaryRow(place.name||'Unnamed place',[place.home?'Home':'',`${durationText(place.observed_minutes)} observed`,`${place.visits} visit${place.visits===1?'':'s'}`,
    Object.entries(place.activities||{}).map(([mode,count])=>modeName(mode)+(count>1?` ×${count}`:'')).join(', '),place.address&&place.address!==place.name?place.address:''].filter(Boolean).join(' · ')));
  const reminders=[...(value.reminders_triggered||[]).map(item=>summaryRow(item.text,`Triggered ${clock(item.triggered_at)} · ${item.place_name||''}`)),
    ...(value.reminders_created||[]).map(item=>summaryRow(item.text,`Created ${clock(item.created_at)} · ${item.place_name||''}`)),
    ...(value.reminders_cancelled||[]).map(item=>summaryRow(item.text,`Cancelled ${clock(item.cancelled_at)} · ${item.place_name||''}`)),
    ...(value.active_reminders_at_end||[]).map(item=>summaryRow(item.text,`Still active at the end of the day · ${item.place_name||''}`))];
  const gaps=(value.coverage_gaps||[]).map(gap=>summaryRow('No location data',`${clock(gap.start)} – ${clock(gap.end)} · ${durationText(gap.minutes)}`));
  // Rough token estimate (about 4 characters each) to show what the brief saves an AI tool.
  const tokens=text=>'≈'+Math.round(text.length/4).toLocaleString()+' tokens',brief=record.brief;
  const briefBlock=`<section class="summary-block summary-brief"><div class="brief-head"><h3>Brief for AI tools</h3>${brief?`<small>${tokens(JSON.stringify(brief))} · full record ${tokens(JSON.stringify(value))}</small><button type="button" class="outline-button" id="copy-brief">Copy</button>`:''}</div>
    ${brief?`<pre>${safe(JSON.stringify(brief,null,2))}</pre>`:'<p class="empty">No brief is stored with this record yet. Refresh to build it.</p>'}</section>`;
  $('summary-detail').innerHTML=`<div class="summary-head"><div><h2>${safe(longDate(record.date))}</h2><small>${safe(record.timezone)} · stored ${safe(formatTime(record.updated_at))}${record.created_at!==record.updated_at?' · first written '+safe(formatTime(record.created_at)):''} · schema ${safe(value.schema_version??'?')}</small></div>
      <div class="summary-actions"><button type="button" class="outline-button" id="summary-history">Open in History</button><button type="button" class="outline-button" id="summary-refresh">Refresh</button></div></div>
    ${record.stale?'<p class="terms-note">The recorded data for this day changed after this record was stored. Refresh to rebuild it.</p>':''}
    <p class="summary-overview">${safe(value.overview||'')}</p>
    <dl class="summary-facts">${facts.map(([label,text])=>`<div><dt>${safe(label)}</dt><dd>${safe(text)}</dd></div>`).join('')}</dl>
    ${activity?`<div class="activity-chips">${activity}</div>`:''}
    ${briefBlock}
    ${summarySection('Visits',visits,'No stays were stored for this day.')}
    ${summarySection('Trips',trips,'No trips.')}
    ${summarySection('Places',places,'No places.')}
    ${summarySection('Reminders',reminders,'No reminder activity.')}
    ${summarySection('Coverage',gaps,value.coverage_note||'No gaps over 30 minutes.')}
    <details class="summary-json"><summary>Stored JSON</summary><pre>${safe(JSON.stringify(value,null,2))}</pre></details>`;
  $('summary-history').onclick=()=>{state.focusPlace=null;state.followToday=record.date===localDateKey();$('day-date').value=record.date;showView('explore');refreshExplore().catch(error=>toast(error.message))};
  $('summary-refresh').onclick=()=>openSummary(record.date,true);
  if(brief)$('copy-brief').onclick=()=>copyText(JSON.stringify(brief,null,2),'Brief copied');
}
function renderPlacesProvider(){
  const providers=state.status?.places_providers||[],chosen=state.settings?.places_provider||'';
  const select=$('places-provider');
  select.innerHTML='<option value="">Automatic · first configured</option>'+providers.map(provider=>
    `<option value="${safe(provider.id)}"${provider.configured||provider.id===chosen?'':' disabled'}>${safe(provider.label)}${provider.configured?'':' · set '+safe(provider.env)}</option>`).join('');
  select.value=chosen;
  const active=providers.find(provider=>provider.id===state.status?.geocoder);
  $('places-provider-help').textContent=active?`Currently using ${active.label}.`:'No provider is configured; set an API key in .env and restart the server.';
  updateProviderNotice();
}
function updateProviderNotice(){
  // Automatic resolves to whatever the server chose, so check that too.
  const value=$('places-provider').value||state.status?.geocoder;
  $('mapbox-terms').hidden=value!=='mapbox';
}
function renderSettings(){if(!state.settings)return;renderPlacesProvider();populateTimezones(state.settings.timezone);$('speed-strategy').value=state.settings.speed_strategy;$('speed-offset').value=state.settings.reported_speed_offset_kmh;updateSpeedSettingsVisibility();renderDailySummaries()}
function updateSpeedSettingsVisibility(){const reported=$('speed-strategy').value==='reported';$('speed-offset').disabled=!reported;$('speed-offset-field').classList.toggle('disabled-field',!reported)}
async function refreshCurrent(){state.current=await api('/api/current');renderCurrent();drawMap()}
async function refreshAll(){try{const [status,current,settings,dailySummaries]=await Promise.all([api('/api/status'),api('/api/current'),api('/api/settings'),api('/api/daily-summaries?limit=10')]);state.status=status;state.current=current;state.settings=settings;state.dailySummaries=dailySummaries;if(state.followToday)$('day-date').value=localDateKey();renderCurrent();renderSettings();$('connection').textContent='Connected';$('geocode-hint').textContent=status.geocoding?(current?'Results favor your last recorded location.':'Searches are worldwide until a location is recorded.'):'Place search needs a geocoding provider.';await refreshMeta();await Promise.all([refreshExplore(),refreshInsights(),refreshCoverage()])}catch(e){toast(e.message)}}
async function pollEvents(){try{const events=await api('/api/events?since_id='+state.lastEventId);const newest=events.reduce((max,e)=>Math.max(max,e.id),state.lastEventId);if(state.lastEventId&&events.length){for(const event of events.reverse()){toast(`${event.text} · ${event.place_name}`);if(Notification.permission==='granted')new Notification('Location reminder',{body:`${event.text} · ${event.place_name}`})}}state.lastEventId=newest;if(events.length)await refreshMeta()}catch(e){/* Connection errors are surfaced on explicit actions. */}}
$('auth-cancel').onclick=()=>{authDismissed=true;$('auth-panel').hidden=true;$('auth-token').value=''};
$('auth-form').onsubmit=async event=>{event.preventDefault();const token=$('auth-token').value.trim();try{const response=await fetch('/api/session',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const result=await response.json();if(!response.ok)throw Error(result.error||'Access token rejected');sessionStorage.removeItem('geotracker-token');$('auth-token').value='';$('auth-panel').hidden=true;authDismissed=false;await refreshAll()}catch(error){$('auth-error').textContent=error.message}};
document.querySelectorAll('.detail-tab').forEach(button=>button.onclick=()=>{const name=button.dataset.panel;document.querySelectorAll('.detail-tab').forEach(tab=>{const active=tab===button;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active))});document.querySelectorAll('.detail-panel').forEach(panel=>panel.classList.toggle('active',panel.dataset.panelView===name))});
document.querySelectorAll('[data-sample-view]').forEach(button=>button.onclick=()=>{state.sampleView=button.dataset.sampleView;document.querySelectorAll('[data-sample-view]').forEach(tab=>tab.classList.toggle('active',tab===button));renderSamples()});
$('raw-previous').onclick=async()=>{if(state.rawPage>0){state.rawPage--;try{await refreshRawPage()}catch(error){toast(error.message)}}};
$('raw-next').onclick=async()=>{if((state.rawPage+1)*state.rawPageSize<state.rawTotal){state.rawPage++;try{await refreshRawPage()}catch(error){toast(error.message)}}};
document.querySelectorAll('[data-places-tab]').forEach(button=>button.onclick=()=>{const tab=button.dataset.placesTab;document.querySelectorAll('[data-places-tab]').forEach(item=>{const active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-selected',String(active))});document.querySelectorAll('[data-places-panel]').forEach(panel=>panel.classList.toggle('active',panel.dataset.placesPanel===tab))});
function moveDay(offset){
  state.focusPlace=null;
  $('day-date').value=shiftDateKey($('day-date').value,offset);
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
$('locate').onclick=()=>{const point=state.current||state.day?.last_location;if(point&&map)map.setView([point.lat,point.lon],15)};
$('current-coordinates').onclick=()=>{if(state.current)copyText(coordinateText(state.current.lat,state.current.lon),'Coordinates copied')};
$('export').onclick=async()=>{try{const q=params().toString();const data=await api('/api/export.geojson'+(q?'?'+q:''));const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/geo+json'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='geotracker-export.geojson';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000)}catch(e){toast(e.message)}};
$('search-form').onsubmit=async e=>{e.preventDefault();try{const q=new URLSearchParams({q:$('search').value});state.current=await api('/api/current');if(state.current){q.set('lat',state.current.lat);q.set('lon',state.current.lon)}const results=await api('/api/geocode?'+q);$('search-results').innerHTML=results.map((p,i)=>`<div class="result-item"><div class="place-main">${placeIcon(p)}<div><strong>${safe(p.name)}</strong><span>${safe(p.address||'')} · ${Number(p.lat).toFixed(5)}, ${Number(p.lon).toFixed(5)}</span></div></div><button data-search="${i}">Use</button></div>`).join('')||'<p class="empty">No matches.</p>';$('search-results').querySelectorAll('[data-search]').forEach(b=>b.onclick=()=>{const p=results[+b.dataset.search];$('place-id').value='';$('place-category').value=p.category||'';$('place-provider-id').value=p.provider_place_id||'';$('place-name').value=p.name.slice(0,120);$('place-lat').value=p.lat;$('place-lon').value=p.lon;$('place-address').value=p.address||''});if(map&&results[0]){searchLayer.clearLayers();L.marker([results[0].lat,results[0].lon]).addTo(searchLayer);map.setView([results[0].lat,results[0].lon],13)}}catch(err){toast(err.message)}};
$('place-form').onsubmit=async e=>{e.preventDefault();try{await post('/api/places',{...($('place-id').value?{id:+$('place-id').value}:{}),name:$('place-name').value,lat:+$('place-lat').value,lon:+$('place-lon').value,radius_m:+$('place-radius').value,address:$('place-address').value,category:$('place-category').value,provider_place_id:$('place-provider-id').value});$('place-dialog').close();await refreshMeta();await Promise.all([refreshExplore(),refreshInsights()]);toast('Place saved')}catch(err){toast(err.message)}};
$('reverse').onclick=async()=>{if(!$('place-lat').value||!$('place-lon').value){toast('Enter coordinates first');return}try{const q=new URLSearchParams({lat:$('place-lat').value,lon:$('place-lon').value});const result=await api('/api/reverse?'+q);$('place-address').value=result.address||'';$('place-category').value=result.category||'';$('place-provider-id').value=result.provider_place_id||'';if(!$('place-name').value)$('place-name').value=(result.name||'').slice(0,120);toast(result.approximate?'Nearby cached place found — verify before saving':'Place found')}catch(err){toast(err.message)}};
$('add-place').onclick=()=>openPlaceDialog();
$('explore-summaries').onclick=()=>showView('summaries');
$('summary-open-form').onsubmit=event=>{event.preventDefault();const date=$('summary-date').value;if(date)openSummary(date,!state.summaryCatalog?.some(item=>item.date===date))};
$('label-form').onsubmit=async event=>{
  event.preventDefault();
  const target=state.labelEdit;if(!target)return;
  const onlyVisits=document.querySelector('input[name="label-scope"]:checked').value==='visit'&&target.visits.length;
  // A visit-only fix covers the visits' time window, and their device when they share one.
  const sources=[...new Set(target.visits.map(stay=>stay.source))];
  const window=onlyVisits?{started_at:target.visits.map(stay=>stay.started_at).sort()[0],ended_at:target.visits.map(stay=>stay.ended_at).sort().at(-1),source:sources.length===1?sources[0]:''}:{};
  try{
    await post('/api/place-labels',{...(target.editing?{id:target.editing}:{}),...window,name:$('label-name').value,lat:target.lat,lon:target.lon,radius_m:target.radius_m,
      address:$('label-address').value,category:$('label-category').value,provider_place_id:$('label-provider-id').value});
    $('label-dialog').close();
    await afterPlaceNamesChanged('Name saved for this place');
  }catch(error){toast(error.message)}
};
$('reset-label').onclick=async()=>{
  const target=state.labelEdit;if(!target?.labelIds.length)return;
  try{for(const id of target.labelIds)await post('/api/place-labels/delete',{id});$('label-dialog').close();await afterPlaceNamesChanged('Automatic name restored')}catch(error){toast(error.message)}
};
$('close-label-dialog').onclick=$('cancel-label').onclick=()=>$('label-dialog').close();
$('label-dialog').addEventListener('click',event=>{if(event.target===$('label-dialog'))$('label-dialog').close()});
$('close-place-dialog').onclick=$('cancel-place').onclick=()=>$('place-dialog').close();
$('place-dialog').addEventListener('click',event=>{if(event.target===$('place-dialog'))$('place-dialog').close()});
$('speed-strategy').onchange=updateSpeedSettingsVisibility;
$('places-provider').onchange=updateProviderNotice;
$('places-form').onsubmit=async event=>{
  event.preventDefault();
  try{
    state.settings=await post('/api/settings',{places_provider:$('places-provider').value});
    state.status=await api('/api/status');
    renderSettings();
    toast('Places provider saved');
    // Names can change with the provider; refresh what shows them.
    await refreshMeta();await Promise.all([refreshExplore(false),refreshInsights()]);
  }catch(error){toast(error.message)}
};
$('settings-form').onsubmit=async event=>{event.preventDefault();try{state.settings=await post('/api/settings',{timezone:$('summary-timezone').value.trim(),speed_strategy:$('speed-strategy').value,reported_speed_offset_kmh:+$('speed-offset').value});if(state.followToday)$('day-date').value=localDateKey();renderSettings();await refreshExplore();toast('Settings saved')}catch(error){toast(error.message)}};
$('day-date').value=localDateKey();
$('reminder-place').onchange=toggleReminderDestination;
$('reminder-search-button').onclick=async()=>{const query=$('reminder-place-search').value.trim();if(!query){toast('Enter a place name');return}try{const q=new URLSearchParams({q:query});if(state.current){q.set('lat',state.current.lat);q.set('lon',state.current.lon)}const results=await api('/api/geocode?'+q);$('reminder-search-results').innerHTML=results.map((place,index)=>`<div class="result-item"><div class="place-main">${placeIcon(place)}<div><strong>${safe(place.name)}</strong><span>${safe(place.address||'')} · ${Number(place.lat).toFixed(5)}, ${Number(place.lon).toFixed(5)}</span></div></div><button type="button" data-reminder-result="${index}">Use</button></div>`).join('')||'<p class="empty">No matches.</p>';$('reminder-search-results').querySelectorAll('[data-reminder-result]').forEach(button=>button.onclick=()=>setReminderDestination(results[+button.dataset.reminderResult]))}catch(error){toast(error.message)}};
$('reminder-use-current').onclick=()=>{if(!state.current){toast('No current location is available');return}setReminderDestination({...state.current,name:state.current.place_name||state.current.address||'Current location'});toast('Current coordinates selected')};
$('reminder-reverse').onclick=async()=>{if(!$('reminder-lat').value||!$('reminder-lon').value){toast('Enter coordinates first');return}try{const q=new URLSearchParams({lat:$('reminder-lat').value,lon:$('reminder-lon').value});const result=await api('/api/reverse?'+q);setReminderDestination({...result,lat:+$('reminder-lat').value,lon:+$('reminder-lon').value});toast(result.approximate?'Nearby cached place found — verify before creating':'Place found')}catch(error){toast(error.message)}};
$('reminder-form').onsubmit=async e=>{e.preventDefault();try{const custom=$('reminder-place').value==='other';const payload=$('reminder-place').value==='named'?{text:$('reminder-text').value,match_name:$('reminder-match-name').value}:custom?{text:$('reminder-text').value,place_name:$('reminder-place-name').value,lat:+$('reminder-lat').value,lon:+$('reminder-lon').value,radius_m:+$('reminder-radius').value,address:$('reminder-address').value,category:$('reminder-category').value,provider_place_id:$('reminder-provider-id').value}:{text:$('reminder-text').value,place_id:+$('reminder-place').value};await post('/api/reminders',payload);$('reminder-form').reset();$('reminder-search-results').innerHTML='';toggleReminderDestination();await refreshMeta();toast('Reminder created')}catch(err){toast(err.message)}};
$('notifications').onclick=async()=>{if(!('Notification' in window)){toast('Browser notifications are unavailable');return}const permission=await Notification.requestPermission();toast(permission==='granted'?'Browser alerts enabled':'Browser alerts were not enabled')};
function dateLabel(value){const [year,month,day]=value.split('-').map(Number);return new Date(year,month-1,day).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}
function renderVisitResults(result){
  const box=$('visit-results');
  if(!result.visits.length){box.innerHTML=`<p class="empty">No visits matched “${safe(result.query)}”. Try a brand, a place type such as supermarket or park, or a street name.</p>`;return}
  const names=result.places.map(place=>safe(place.name)).join(', ');
  const shown=result.total_visits>result.visits.length?` · latest ${result.visits.length} shown`:'';
  box.innerHTML=`<p class="result-summary"><strong>${result.total_visits} visit${result.total_visits===1?'':'s'}</strong> to ${names}${shown}</p><ul class="visit-list">${result.visits.map((visit,index)=>{
    const details=[`${clock(visit.started_at)} – ${clock(visit.ended_at)}`,durationText(visit.observed_minutes)];
    if(visit.activity)details.push(modeName(visit.activity));
    if(visit.ongoing)details.push('now');
    return `<li><div class="visit-date"><strong>${safe(dateLabel(visit.date))}</strong><span>${safe(visit.weekday)}</span></div><div class="visit-info"><strong>${safe(visit.place)}</strong><small>${safe(details.join(' · '))}</small></div><button type="button" class="outline-button" data-visit-day="${index}">Show day</button></li>`}).join('')}</ul>`;
  box.querySelectorAll('[data-visit-day]').forEach(button=>button.onclick=()=>openVisitDay(result.visits[+button.dataset.visitDay]));
}
function openVisitDay(visit){
  state.pendingStayId=visit.stay_id;
  state.focusPlace=null;
  $('source').value='';
  $('day-date').value=visit.date;
  state.followToday=visit.date===localDateKey();
  showView('explore');
  window.scrollTo({top:0,behavior:'smooth'});
  refreshExplore().catch(error=>toast(error.message));
}
$('visit-search-form').onsubmit=async event=>{
  event.preventDefault();
  const query=$('visit-search').value.trim();
  if(!query)return;
  const box=$('visit-results');
  box.innerHTML='<p class="empty">Searching…</p>';
  try{renderVisitResults(await api('/api/visits?'+new URLSearchParams({q:query,limit:50})))}catch(error){box.innerHTML='';toast(error.message)}
};
$('fit-day').onclick=()=>{state.selected=null;drawSelection(false);fitDay()};
$('close-map-detail').onclick=()=>{$('map-detail-section').hidden=true};
document.addEventListener('keydown',event=>{
  if(event.defaultPrevented||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey)return;
  if(!$('explore-view').classList.contains('active')||!$('auth-panel').hidden||$('place-dialog').open)return;
  if(event.target.closest('input,select,textarea,#map'))return;
  if(event.key==='ArrowLeft'){event.preventDefault();moveDay(-1)}
  else if(event.key==='ArrowRight'&&!$('next-day').disabled){event.preventDefault();moveDay(1)}
  else if(event.key==='Escape'&&state.selected){state.selected=null;drawSelection(false);fitDay()}
});
function linkedDate(){const match=location.hash.match(/date=(\d{4}-\d{2}-\d{2})/);return match&&match[1]<=localDateKey()?match[1]:null}
window.addEventListener('hashchange',()=>{const date=linkedDate();if(date&&date!==$('day-date').value){state.focusPlace=null;state.followToday=false;$('day-date').value=date;showView('explore');refreshExplore().catch(error=>toast(error.message))}});
if(linkedDate()){state.followToday=false;$('day-date').value=linkedDate()}
initMap();refreshAll().then(()=>{state.lastEventId=state.events.reduce((max,e)=>Math.max(max,e.id),0)});setInterval(pollEvents,10000);setInterval(()=>{if(state.followToday&&$('day-date').value!==localDateKey())$('day-date').value=localDateKey();refreshCurrent().catch(()=>{});refreshExplore(false).catch(()=>{})},60000);setInterval(()=>refreshInsights().catch(()=>{}),300000);
