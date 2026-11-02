"""Hardware list prices, read from the manufacturers' own price books.

A price here is never written by a model. Each book is read once into rows --
one row per printed price, with the page it was printed on -- and a request is
answered by finding rows and adding them up. A row that cannot be found is
reported as such; there is no fallback number.

    readers/   one module per page layout; a layout is shared across brands
    lookup     a hardware item in, a price with its parts and pages out
    store      the rows in Supabase
"""
