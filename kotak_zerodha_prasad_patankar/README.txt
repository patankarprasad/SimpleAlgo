
if you want entries with premium stop-losses, on strategy page, make corresponding changes
	Sometimes, we need to use different variations of "place_order" functions (a la PEB and PEB with SL)	
	In reconcile.py, STRATS with "sl" : "None OR *_PSL" will have to be commented or uncommented
	If you want PSL, then "build_position" will decide active position based on the SL tag of that STRAT
	If you don't want PSL, then "build_position" will completely neglect the SL for that STRAT 
	In "reconcile" function, REC_STRATS, will also have to be commented or uncommented accordingly
	Because "reconcile" function closes the position if stoploss is missing (in case of vps failure during exit)



if you want entries with hedge, on strategy page, make corresponding changes
	Sometimes, we need to use different variations of "place_order" functions ( a la RCE )
	In "build_position" function, in STRATS, comment or uncomment accordingly
	If you want entry-wise hedge or persistent hedge, make your selection before finding validity in "build_positions" function
	that selection would affect ORPHAN HEDGE handling in "reconcile" function


if you dont want hedges at all then, on strategy page, make corresponding changes
	Sometimes, we need to use different variations of "place_order" functions ( a la RCE )
	In "build_position" function, in STRATS, comment or uncomment accordingly
	Then comment-out every line in STRATS, REC_STRATS to remove HEDGE orders
	