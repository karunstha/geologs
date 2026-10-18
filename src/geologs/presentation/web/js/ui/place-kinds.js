// Kinds of place, told from a place's categories (Geoapify "catering.cafe", Photon "amenity.fuel",
// Mapbox "gas_station") or, failing those, its name ("Starbucks", "Chase Bank").
// Earlier kinds win, so the specific ones (pizza) come before the general ones (restaurant).
// Each entry: [kind, label, colour, words, brands (names only), icon].
const RAW = [
  ["fuel", "Gas station", "#c4532a", "fuel, gas station, petrol, petrol station, filling station, gas, gasoline",
    "shell, exxon, exxonmobil, chevron, valero, bp, mobil, citgo, marathon, sunoco, phillips 66, conoco, murphy usa, circle k, sinclair, speedway, quiktrip, wawa, sheetz, racetrac, caseys, loves travel, pilot travel, flying j, bucees, kum go, texaco, arco, esso, maverik, holiday stationstores",
    '<path d="M5 20V5a1.5 1.5 0 0 1 1.5-1.5h6A1.5 1.5 0 0 1 14 5v15M3.5 20h12M5 10h9"/><path d="M14 8.5h1.5A1.5 1.5 0 0 1 17 10v6.5a1.5 1.5 0 0 0 3 0V8.5L17.5 6"/>'],
  ["charging", "EV charging", "#2b9a6b", "charging station, charging, ev charger, ev charging, car charging",
    "supercharger, chargepoint, electrify america, evgo, blink charging",
    '<path d="M13 3 5.5 13.5H12L11 21l7.5-10.5H12L13 3Z"/>'],
  ["bank", "Bank", "#2c6e63", "bank, atm, financial, credit union, money transfer, bureau de change, money lender, check cashing",
    "chase, wells fargo, bank of america, citibank, pnc, td bank, capital one, truist, arvest, simmons bank, hsbc, barclays, santander, western union, moneygram",
    '<path d="M3.5 9.5 12 4l8.5 5.5M5 9.5h14M6.5 10v7M10 10v7M14 10v7M17.5 10v7M4 20h16M4.5 17.5h15"/>'],
  ["post", "Post office", "#b8862b", "post office, post box, postal, courier, shipping, parcel locker, mail",
    "usps, ups store, the ups store, fedex, fedex office, dhl",
    '<rect x="3.5" y="5.5" width="17" height="13" rx="2"/><path d="m4 7 8 6 8-6"/>'],
  ["cafe", "Café", "#8a5a3c", "cafe, coffee, coffee shop, coffeehouse, tea, tea house, bubble tea, espresso, roastery",
    "starbucks, dunkin, dunkin donuts, tim hortons, dutch bros, costa, peets, caribou, scooters coffee, biggby, blue bottle",
    '<path d="M4.5 9h12v5a5 5 0 0 1-5 5h-2a5 5 0 0 1-5-5V9Z"/><path d="M16.5 10.5h1.2a2.3 2.3 0 0 1 0 4.6h-1.5M8 3.5c-.6 1 .6 1.8 0 3M11.5 3.5c-.6 1 .6 1.8 0 3"/>'],
  ["bakery", "Bakery", "#b5763a", "bakery, pastry, patisserie, donut, doughnut, confectionery, cake, cupcake, bagel, dessert, chocolate",
    "krispy kreme, nothing bundt, crumbl, einstein bros",
    '<path d="M6 12h12l-1.5 8h-9L6 12Z"/><path d="M5 12a3 3 0 0 1 2.5-4.3 4.5 4.5 0 0 1 9 0A3 3 0 0 1 19 12M10 12l.5 8M14 12l-.5 8"/>'],
  ["ice_cream", "Ice cream", "#d0679a", "ice cream, frozen yogurt, frozen yoghurt, gelato, frozen dessert",
    "baskin robbins, dairy queen, cold stone, menchies, braums, culvers, yogurtland",
    '<path d="M7.5 10.5 12 21l4.5-10.5"/><path d="M7 10.5a5 5 0 1 1 10 0Z"/>'],
  ["fast_food", "Fast food", "#d35400", "fast food, burger, hamburger, pizza, sandwich, takeaway, take away, fried chicken, hot dog, kebab, taco, food truck, drive through, wings",
    "mcdonalds, burger king, wendys, taco bell, kfc, chick fil a, popeyes, subway, sonic, sonic drive in, arbys, chipotle, five guys, whataburger, jack in the box, dominos, pizza hut, papa johns, little caesars, panda express, raising canes, zaxbys, hardees, carls jr, jimmy johns, jersey mikes, firehouse subs, in n out, shake shack, wingstop, qdoba, del taco, white castle, krystal, church s chicken, bojangles, slim chicken",
    '<path d="M4.5 10.5a7.5 5.5 0 0 1 15 0Z"/><path d="M4 13.5h16M5.5 16.5h13a2.5 2.5 0 0 1-2.5 2.5H8a2.5 2.5 0 0 1-2.5-2.5Z"/>'],
  ["grocery", "Grocery", "#2f8f4e", "supermarket, grocery, groceries, greengrocer, farmers market, farm shop, butcher, deli, delicatessen, organic, health food, hypermarket, asian grocery",
    "kroger, aldi, lidl, whole foods, trader joes, safeway, publix, heb, h e b, albertsons, food lion, wegmans, sprouts, meijer, harris teeter, piggly wiggly, hy vee, winco, fresh market, the fresh market, natural grocers, tesco, sainsburys, asda, wholefoods, food 4 less, fred meyer, ralphs, smiths, kings soopers, giant eagle, stop shop, market basket, save a lot, walmart neighborhood market, harps, edwards food giant",
    '<path d="M3.5 9.5h17l-1.8 9a1.5 1.5 0 0 1-1.5 1.2H6.8a1.5 1.5 0 0 1-1.5-1.2l-1.8-9Z"/><path d="m8 9.5 3-5.5M16 9.5l-3-5.5M9 13v3.5M12 13v3.5M15 13v3.5"/>'],
  ["convenience", "Convenience store", "#3b7fa8", "convenience, convenience store, kiosk, newsagent, tobacco, smoke shop, vape, liquor, liquor store, wine shop, off licence, alcohol, beverages",
    "7 eleven, seven eleven, familymart, lawson, total wine, specs",
    '<path d="M4 9.5V20h16V9.5M3 4.5h18l-1 5H4l-1-5Z"/><path d="M10 20v-5h4v5"/>'],
  ["restaurant", "Restaurant", "#c0612a", "restaurant, catering, food, diner, eatery, bistro, steakhouse, steak house, grill, buffet, food court, brasserie, trattoria, ramen, sushi, noodle, barbecue, bbq, seafood, cuisine, kitchen",
    "olive garden, applebees, chilis, ihop, dennys, cracker barrel, waffle house, texas roadhouse, outback, red lobster, red robin, buffalo wild wings, cheesecake factory, longhorn steakhouse, panera, panera bread, chuys, pf changs",
    '<path d="M7 3v8M4 3v5c0 2 1 3 3 3s3-1 3-3V3M7 11v10M16 3v18M16 3c3 1 4 4 4 7h-4"/>'],
  ["bar", "Bar", "#a0652f", "bar, pub, nightclub, night club, biergarten, beer garden, brewery, taproom, tap room, wine bar, winery, lounge, tavern, brewpub, cocktail, saloon, distillery, cidery",
    "",
    '<path d="M5 4h14l-7 8-7-8Z"/><path d="M12 12v7M8 20h8"/>'],
  ["pets", "Pets", "#9a6a3a", "veterinary, vet, veterinarian, animal hospital, animal clinic, pet, pet store, pet shop, pet grooming, pet supply, animal shelter, animal boarding, dog grooming, kennel",
    "petsmart, petco, banfield, vca",
    '<circle cx="6.5" cy="10.5" r="1.8"/><circle cx="10" cy="6" r="1.8"/><circle cx="14.5" cy="6" r="1.8"/><circle cx="18" cy="10.5" r="1.8"/><path d="M12.2 11.5c-2.5 0-5 3.5-5 6a2.5 2.5 0 0 0 2.5 2.5c1 0 1.6-.5 2.5-.5s1.5.5 2.5.5a2.5 2.5 0 0 0 2.5-2.5c0-2.5-2.5-6-5-6Z"/>'],
  ["pharmacy", "Pharmacy", "#d14b5a", "pharmacy, chemist, drugstore, drug store, apothecary",
    "cvs, walgreens, rite aid, boots, duane reade",
    '<path d="M10.2 4.6a4.2 4.2 0 0 1 6 6L10.6 16.2a4.2 4.2 0 0 1-6-6Z"/><path d="m7.4 7.4 6 6"/>'],
  ["dentist", "Dentist", "#4a93b8", "dentist, dental, orthodontist, orthodontics, endodontist",
    "aspen dental",
    '<path d="M7.5 4C5 4 3.5 6 3.5 8.5c0 3 1.5 4.5 2 7.5.4 2.5 1 4.5 2.3 4.5 1.5 0 1.6-4.5 4.2-4.5s2.7 4.5 4.2 4.5c1.3 0 1.9-2 2.3-4.5.5-3 2-4.5 2-7.5C20.5 6 19 4 16.5 4c-2 0-2.7 1-4.5 1S9.5 4 7.5 4Z"/>'],
  ["health", "Health", "#c24646", "hospital, emergency, urgent care, clinic, doctor, doctors, healthcare, health care, medical, physician, health centre, health center, laboratory, physiotherapist, physical therapy, chiropractor, optician, optometrist, eye care, hearing, dialysis, blood bank, nursing home, hospice, therapist, psychologist, counselling, counseling",
    "baptist health, uams, mayo clinic, kaiser permanente, minuteclinic, labcorp, quest diagnostics",
    '<path d="M12 21S4 16 4 9a4 4 0 0 1 7-2.7L12 7.5l1-1.2A4 4 0 0 1 20 9c0 7-8 12-8 12Z"/><path d="M9 12h6M12 9v6"/>'],
  ["salon", "Salon", "#a14c7c", "hair, hairdresser, hair salon, salon, beauty, beauty salon, barber, barbershop, nail, nail salon, cosmetics, massage, spa, day spa, tattoo, piercing, tanning, waxing, lash, brow, esthetician, makeup",
    "great clips, supercuts, sport clips, ulta, ulta beauty, sephora, fantastic sams, european wax center, massage envy, sally beauty",
    '<circle cx="6" cy="7" r="3"/><circle cx="6" cy="17" r="3"/><path d="m8.7 8.3 11.8 7.2M8.7 15.7 20.5 8.5"/>'],
  ["gym", "Gym", "#cc4b37", "gym, fitness, fitness centre, fitness center, health club, sports centre, sports center, recreation center, yoga, pilates, crossfit, martial arts, dojo, karate, boxing, spin, cycling studio, climbing gym, bouldering",
    "planet fitness, la fitness, anytime fitness, orangetheory, 24 hour fitness, golds gym, ymca, ywca, crunch fitness, lifetime fitness, life time, equinox, f45, snap fitness, workout anytime",
    '<path d="M6.5 7v10M17.5 7v10M3.5 9.5v5M20.5 9.5v5M6.5 12h11"/>'],
  ["sports", "Sports", "#2f7d6d", "stadium, sports, pitch, golf, golf course, driving range, tennis, swimming pool, swimming, swimming area, aquatic, ice rink, skating rink, baseball, softball, football, soccer, basketball, volleyball, pickleball, racetrack, race track, arena, ballpark, skatepark, skate park, sports field, athletics",
    "topgolf",
    '<path d="M8 4.5h8v5a4 4 0 0 1-8 0v-5Z"/><path d="M8 6.5H5.5a2.5 2.5 0 0 0 2.6 3.5M16 6.5h2.5a2.5 2.5 0 0 1-2.6 3.5M12 13.5V17M8.5 20.5h7M9.5 17h5v3.5h-5z"/>'],
  ["entertainment", "Fun", "#d4497a", "amusement park, theme park, zoo, aquarium, water park, waterpark, bowling, bowling alley, arcade, escape room, casino, miniature golf, mini golf, trampoline, entertainment, amusement, fairground, go kart, karting, laser tag, roller rink, trampoline park, wildlife park, safari, petting zoo, playland",
    "dave busters, chuck e cheese, main event, round 1, sky zone, urban air",
    '<circle cx="12" cy="10" r="6.5"/><path d="M12 3.5v13M5.5 10h13M7.4 5.4l9.2 9.2M16.6 5.4l-9.2 9.2M8 21l4-4.5 4 4.5M6.5 21h11"/>'],
  ["cinema", "Cinema & theatre", "#a8326e", "cinema, movie, movies, movie theater, movie theatre, theatre, theater, concert hall, music venue, opera, comedy club, performing arts, amphitheatre, amphitheater, drive in, auditorium, philharmonic, symphony",
    "amc, regal, cinemark, alamo drafthouse, marcus theatres, harkins",
    '<path d="M4 7.5A1.5 1.5 0 0 1 5.5 6h13A1.5 1.5 0 0 1 20 7.5V10a2 2 0 0 0 0 4v2.5a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 16.5V14a2 2 0 0 0 0-4V7.5Z"/><path d="M14 6v2M14 11v2M14 16v2"/>'],
  ["hotel", "Hotel", "#5a6fb5", "hotel, motel, hostel, guest house, guesthouse, inn, resort, accommodation, lodging, lodge, bed and breakfast, b b, chalet, holiday home, vacation rental, serviced apartment",
    "hilton, marriott, holiday inn, hampton inn, la quinta, best western, days inn, super 8, comfort inn, comfort suites, hyatt, airbnb, sheraton, westin, courtyard, fairfield inn, motel 6, red roof, quality inn, embassy suites, doubletree, residence inn, homewood suites, springhill suites, candlewood, extended stay, wyndham, radisson, ihg, home2 suites, four seasons, ritz carlton",
    '<path d="M3 18.5V6M3 14.5h18v4M21 14.5V12a2.5 2.5 0 0 0-2.5-2.5H11v5"/><circle cx="7" cy="11.5" r="1.8"/>'],
  ["camping", "Camping", "#557a3a", "camp site, campsite, camping, campground, caravan site, rv park, caravan park, camp, glamping, trailer park",
    "koa, kampgrounds of america",
    '<path d="M3 20h18M4.5 20 12 5l7.5 15M9 20l3-5.5 3 5.5"/>'],
  ["museum", "Museum & sights", "#8c5a2b", "museum, gallery, art gallery, arts centre, arts center, arts, monument, memorial, castle, historic, heritage, attraction, landmark, sights, viewpoint, observatory, planetarium, ruins, archaeological site, lighthouse, palace, exhibition, visitor centre, visitor center",
    "smithsonian",
    '<rect x="3.5" y="4.5" width="17" height="15" rx="1.5"/><path d="m3.5 16 5-5 4 4 2.5-2.5 5.5 5"/><circle cx="15.5" cy="8.5" r="1.5"/>'],
  ["library", "Library & books", "#6a5acd", "library, public library, bookshop, bookstore, books, book store, reading room, archive",
    "barnes noble, barnes and noble, half price books, waterstones, books a million",
    '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5v-13ZM20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5v-13Z"/>'],
  ["education", "School", "#6c55b0", "school, college, university, education, academy, kindergarten, childcare, child care, daycare, day care, preschool, nursery school, campus, institute, high school, elementary school, middle school, driving school, language school, tutoring, music school, dance school",
    "kindercare, goddard school, primrose school, kumon, sylvan",
    '<path d="m3 10 9-5 9 5-9 5-9-5Z"/><path d="M7 12.5V17c3 2 7 2 10 0v-4.5M21 10v6"/>'],
  ["worship", "Place of worship", "#7d5ba6", "place of worship, worship, church, cathedral, chapel, temple, mosque, masjid, synagogue, religion, religious, monastery, shrine, gurdwara, mandir, stupa, kingdom hall, parish, basilica, abbey, ministry, ministries, fellowship, tabernacle, congregation, gompa, vihara",
    "",
    '<path d="M12 2.5v4M10 4.5h4M7 21V11l5-4 5 4v10M4 21h16M10.5 21v-4a1.5 1.5 0 0 1 3 0v4"/>'],
  ["airport", "Airport", "#3567ad", "airport, aerodrome, airfield, heliport, airline, air terminal, international airport, regional airport",
    "",
    '<path d="M10.5 3.5c0-.8.7-1.5 1.5-1.5s1.5.7 1.5 1.5V9l7 4v2l-7-2v4.5l2 1.5v1.5L12 20l-3.5 1v-1.5l2-1.5V13l-7 2v-2l7-4V3.5Z"/>'],
  ["parking", "Parking", "#4a6fa5", "parking, car park, parking lot, parking garage, parking space, multi storey, park and ride, park ride",
    "",
    '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M10 16.5v-9h2.8a2.7 2.7 0 0 1 0 5.4H10"/>'],
  ["car", "Car care", "#556070", "car repair, car wash, car dealer, car dealership, car rental, car hire, vehicle, vehicles, auto, automotive, auto repair, auto parts, tyres, tires, tire shop, mechanic, oil change, motorcycle, body shop, collision, auto body, smog check, inspection station, garage, towing, car sharing",
    "jiffy lube, autozone, oreilly, oreilly auto parts, advance auto parts, napa, pep boys, firestone, discount tire, valvoline, enterprise, hertz, avis, budget, carmax, midas, meineke, les schwab, goodyear, tesla, take 5, mavis",
    '<path d="M14.5 4.5a4.5 4.5 0 0 0-4.2 6.1L4 16.9V20h3.1l6.3-6.3a4.5 4.5 0 0 0 6.1-4.2l-2.7 2.7-2.7-.6-.6-2.7 2.7-2.7a4.5 4.5 0 0 0-1.7-.3Z"/>'],
  ["hardware", "Hardware", "#a46a1f", "hardware, doityourself, do it yourself, diy, home improvement, building materials, garden centre, garden center, lumber, paint, tools, plumbing supply, nursery",
    "home depot, the home depot, lowes, menards, ace hardware, harbor freight, true value, tractor supply, sherwin williams, b q, bunnings, northern tool, 84 lumber",
    '<path d="M14.5 3.5 20.5 9.5 18 12l-6-6 2.5-2.5Z"/><path d="m13 7.5-9.5 9.5a1.8 1.8 0 0 0 2.5 2.5l9.5-9.5"/>'],
  ["electronics", "Electronics", "#3d6fb5", "electronics, mobile phone, mobile phones, computer, computers, telecommunication, telecom, phone shop, phone repair, video games, hifi, camera",
    "best buy, apple store, apple, verizon, at t, t mobile, sprint, gamestop, micro center, cricket wireless, boost mobile, metro by t mobile, us cellular, radioshack, ubreakifix",
    '<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/>'],
  ["clothing", "Clothes", "#b04f7a", "clothes, clothing, fashion, shoes, shoe store, boutique, jewelry, jewellery, jeweler, tailor, apparel, outfitter, bridal, lingerie, sportswear, accessories, watches",
    "tj maxx, ross, ross dress for less, marshalls, old navy, gap, h m, zara, kohls, macys, nordstrom, nordstrom rack, dillards, burlington, nike, adidas, foot locker, famous footwear, dsw, uniqlo, forever 21, american eagle, banana republic, kay jewelers, zales, academy sports, dicks sporting goods, rei, bass pro shops, cabelas, primark",
    '<path d="M8.5 3.5 4 6l-1.5 4 3 1.5v9h13v-9l3-1.5L20 6l-4.5-2.5a3.5 3.5 0 0 1-7 0Z"/>'],
  ["laundry", "Laundry", "#4f8fb0", "laundry, laundromat, launderette, dry cleaning, dry cleaner, dry cleaners, cleaners, wash and fold",
    "",
    '<rect x="4.5" y="3" width="15" height="18" rx="2"/><circle cx="12" cy="13" r="4.5"/><path d="M7.5 6.5h.01M10 6.5h3"/>'],
  ["civic", "Civic", "#4b5d7a", "townhall, town hall, city hall, government, courthouse, court house, police, police station, fire station, fire department, embassy, consulate, dmv, department of motor vehicles, social facility, community centre, community center, public service, public building, county office, social security, passport office, prison, military, post of police, ranger station, polling station, civic",
    "",
    '<path d="M5 21V4M5 4.5h11l-2 3.5 2 3.5H5"/>'],
  ["cemetery", "Cemetery", "#6b6f76", "cemetery, graveyard, grave yard, funeral, funeral home, crematorium, mortuary, memorial park, burial ground",
    "",
    '<path d="M7 20.5V10a5 5 0 0 1 10 0v10.5M4.5 20.5h15M12 9v5M10 11h4"/>'],
  ["beach", "Beach", "#d4a72c", "beach, seaside, coast, shore, coastline, beach resort",
    "",
    '<path d="M4 12a8 8 0 0 1 16 0Z"/><path d="m12 12 2.5 8.5M3 20.5h18"/>'],
  ["outdoors", "Outdoors", "#6b7b3a", "peak, mountain, mountains, hiking, trail, trailhead, hill, summit, cliff, canyon, volcano, cave, scenic overlook, overlook, nature trail, wilderness",
    "",
    '<path d="m3 19.5 6.5-11 4 6.5 2-3 5.5 7.5Z"/><path d="m7.5 12 2 1.5 1.5-1"/>'],
  ["water", "Water", "#1f86ad", "lake, water, reservoir, river, waterfall, falls, pond, marina, dam, spring, hot spring, bay, lagoon, creek, harbour, harbor, pier, boat launch, boat ramp, dock, ferry, ferry terminal, canal, waterfront",
    "",
    '<path d="M12 3s6 6.3 6 11a6 6 0 0 1-12 0c0-4.7 6-11 6-11Z"/><path d="M9 15c.7 1.5 1.8 2.2 3.5 2.2"/>'],
  ["transit", "Transit", "#a83f78", "station, railway, railway station, train, train station, bus station, bus stop, bus terminal, subway station, metro, metro station, tram, tram stop, light rail, public transport, transit, transportation, platform, terminal",
    "amtrak, greyhound, megabus, flixbus",
    '<rect x="5.5" y="3.5" width="13" height="13" rx="3"/><path d="M5.5 10h13M8.5 16.5l-2 4M15.5 16.5l2 4M9 13.3h.01M15 13.3h.01"/>'],
  ["shopping", "Shopping", "#2f6fb0", "mall, shopping, shopping centre, shopping center, shopping mall, department store, retail, shop, store, commercial, outlet, outlet mall, variety store, discount store, market, marketplace, furniture, gift, gifts, toys, crafts, hobby, florist, flowers, sporting goods, second hand, thrift, charity shop, pawn, antiques, music store, wholesale, warehouse club, dollar store, cosmetics store, optics, stationery, office supplies, mattress, appliance, kitchenware, home goods, plaza, superstore",
    "walmart, walmart supercenter, target, costco, sams club, dollar general, dollar tree, family dollar, five below, big lots, ikea, homegoods, home goods, at home, hobby lobby, michaels, joann, bed bath beyond, ollies, tuesday morning, office depot, staples, goodwill, salvation army, savers, bjs, kmart, sears, jcpenney, belk, big 5, world market, pier 1, rooms to go, ashley, mattress firm, amazon",
    '<path d="M3 4h2l2.2 10.2a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 2-1.6L20 8H7"/><circle cx="10" cy="20" r="1"/><circle cx="18" cy="20" r="1"/>'],
  ["work", "Work", "#4f6177", "office, offices, coworking, co working, workplace, business park, corporate, company, headquarters, hq, industrial, factory, warehouse, plant, distribution center, distribution centre, industrial park, business center, business centre, office park, agency, firm, consulting, law firm, attorney, lawyer, insurance, accountant, real estate, realtor, estate agent",
    "state farm, allstate, farmers insurance, edward jones, h r block",
    '<rect x="3.5" y="7.5" width="17" height="12" rx="2"/><path d="M9 7.5v-2A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5v2M3.5 12.5h17"/>'],
  ["park", "Park", "#3f8a45", "park, garden, gardens, playground, nature reserve, dog park, recreation ground, picnic, picnic site, forest, wood, woods, nature, leisure, national park, state park, botanical garden, arboretum, greenway, meadow, orchard",
    "",
    '<path d="M12 21V9M7 13c-3-4-.4-9 5-10 5.4 1 8 6 5 10-1.5 2-3.5 2.5-5 2.5S8.5 15 7 13Z"/>'],
  ["residential", "Residence", "#6b7f8f", "residential, residence, residences, apartment, apartments, house, home, condo, condominium, dormitory, dorm, townhouse, building residential, housing, villa, cottage",
    "",
    '<path d="m3 11 9-8 9 8M5 10v11h14V10M9 21v-7h6v7"/>'],
];

const HOME_ICON = '<path d="m3 11 9-8 9 8M5 10v11h14V10M9 21v-7h6v7"/>';
const PLACE_ICON = '<path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>';

// Text as the kinds read it: lower case, without accents or apostrophes, words split by single spaces.
export function normalText(text) {
  return ` ${String(text || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim()} `;
}

function matcher(list) {
  const words = list
    .split(",")
    .map((word) => normalText(word).trim())
    .filter(Boolean);
  // " word " with an optional plural, against text padded with spaces.
  return words.length ? new RegExp(` (?:${words.join("|")})(?:s|es)? `) : null;
}

export const KINDS = RAW.map(([kind, label, color, words, brands, icon]) => ({
  kind,
  label,
  color,
  icon,
  words: matcher(words),
  brands: matcher(brands),
}));
export const KIND_INFO = Object.fromEntries([
  ["home", { kind: "home", label: "Home", color: "#5b7286", icon: HOME_ICON }],
  ["place", { kind: "place", label: "Place", color: "#0f766e", icon: PLACE_ICON }],
  ...KINDS.map((info) => [info.kind, info]),
]);

const HOME_NAMES = new Set([" home ", " my home ", " my house ", " house ", " home sweet home "]);

// The kind of a place: "home" for one called Home, else from its categories, else its name.
export function kindOf(place = {}) {
  const name = normalText([place.name, place.display_name].filter(Boolean).join(" "));
  const own = normalText(place.name || place.display_name);
  if (HOME_NAMES.has(own) || place.category === "home") return "home";
  const categories = normalText([place.category, ...(place.categories || [])].filter(Boolean).join(" "));
  if (categories.trim()) {
    for (const info of KINDS) if (info.words?.test(categories)) return info.kind;
  }
  // A name that is a street address ("1201 Pine Hill Rd") says nothing about the place.
  if (/^\s*\d/.test(name)) return "place";
  for (const info of KINDS) {
    if (info.brands?.test(name) || info.words?.test(name)) return info.kind;
  }
  return "place";
}
