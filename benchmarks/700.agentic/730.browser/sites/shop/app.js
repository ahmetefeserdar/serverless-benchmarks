// Client-side rendered shop: the catalog is fetched as JSON and all views are built in the
// browser, like in single-page applications. Routes live in the URL hash.
(function () {
  'use strict';

  var catalog = null;
  var byId = {};
  var cart = [];  // [{id, qty}]
  var lastOrder = null;
  var view = document.getElementById('view');

  function money(cents) {
    return 'CHF ' + (cents / 100).toFixed(2);
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
    });
  }

  // Product pictures are generated vector graphics, so the site needs no image files.
  function thumbnail(p) {
    var hue = (p.id * 47) % 360;
    return '<svg viewBox="0 0 160 120" role="img" aria-label="Picture of ' + escapeHtml(p.name) + '">' +
      '<rect width="160" height="120" fill="hsl(' + hue + ',60%,85%)"/>' +
      '<circle cx="' + (40 + p.id % 80) + '" cy="60" r="' + (20 + p.id % 25) + '" fill="hsl(' + ((hue + 120) % 360) + ',55%,55%)"/>' +
      '<polygon points="100,20 150,100 60,100" fill="hsl(' + ((hue + 240) % 360) + ',50%,45%)" opacity="0.7"/>' +
      '</svg>';
  }

  function stars(rating) {
    return '★★★★★'.slice(0, Math.round(rating)) + ' ' + rating.toFixed(1);
  }

  function search(query, category, sort) {
    var q = query.trim().toLowerCase();
    var found = catalog.products.filter(function (p) {
      return (!q || p.name.toLowerCase().indexOf(q) !== -1) && (!category || p.category === category);
    });
    var orders = {
      'price-asc': function (a, b) { return a.price_cents - b.price_cents || a.id - b.id; },
      'price-desc': function (a, b) { return b.price_cents - a.price_cents || a.id - b.id; },
      'rating': function (a, b) { return b.rating - a.rating || a.id - b.id; },
      'relevance': function (a, b) { return a.id - b.id; }
    };
    return found.sort(orders[sort] || orders.relevance);
  }

  function renderList(products, title) {
    var cards = products.map(function (p) {
      return '<article class="product-card" data-id="' + p.id + '">' + thumbnail(p) +
        '<h3>' + escapeHtml(p.name) + '</h3>' +
        '<span class="price">' + money(p.price_cents) + '</span>' +
        '<span class="rating">' + stars(p.rating) + '</span>' +
        '<a class="view" href="#/product/' + p.id + '">View</a></article>';
    });
    view.innerHTML = '<h2>' + escapeHtml(title) + '</h2>' +
      '<p class="summary">' + products.length + ' products</p>' +
      '<section class="grid" id="results">' + cards.join('') + '</section>';
  }

  function renderProduct(p) {
    var reviews = p.reviews.map(function (r) {
      return '<li><strong>' + escapeHtml(r.author) + '</strong> (' + r.stars + '/5): ' + escapeHtml(r.text) + '</li>';
    });
    view.innerHTML = '<article class="product">' + thumbnail(p) + '<div>' +
      '<h1>' + escapeHtml(p.name) + '</h1>' +
      '<p class="price">' + money(p.price_cents) + '</p>' +
      '<p class="rating">' + stars(p.rating) + '</p>' +
      '<p>' + escapeHtml(p.description) + '</p>' +
      '<label for="qty">Quantity</label> <input id="qty" type="number" min="1" value="1"> ' +
      '<button id="add-to-cart">Add to cart</button>' +
      '<p id="cart-status" role="status"></p>' +
      '<h2>Reviews</h2><ul class="reviews">' + reviews.join('') + '</ul></div></article>';
    document.getElementById('add-to-cart').addEventListener('click', function () {
      var qty = Math.max(1, parseInt(document.getElementById('qty').value, 10) || 1);
      cart.push({id: p.id, qty: qty});
      updateCartCount();
      document.getElementById('cart-status').textContent = 'Added ' + qty + ' to cart.';
    });
  }

  function cartTotal() {
    return cart.reduce(function (sum, item) { return sum + byId[item.id].price_cents * item.qty; }, 0);
  }

  function updateCartCount() {
    document.getElementById('cart-count').textContent =
      cart.reduce(function (sum, item) { return sum + item.qty; }, 0);
  }

  function renderCart() {
    var rows = cart.map(function (item) {
      var p = byId[item.id];
      return '<tr><td>' + escapeHtml(p.name) + '</td><td>' + item.qty + '</td><td>' + money(p.price_cents * item.qty) + '</td></tr>';
    });
    view.innerHTML = '<h2>Your cart</h2><table><tr><th>Product</th><th>Quantity</th><th>Price</th></tr>' +
      rows.join('') + '</table><p>Total: ' + money(cartTotal()) + '</p>' +
      '<a id="checkout" class="view" href="#/checkout">Checkout</a>';
  }

  function renderCheckout() {
    view.innerHTML = '<h2>Checkout</h2><form id="checkout-form" onsubmit="return false;">' +
      '<label for="name">Full name</label><input id="name" autocomplete="name">' +
      '<label for="email">Email</label><input id="email" type="email">' +
      '<label for="address">Shipping address</label><textarea id="address"></textarea>' +
      '<button id="place-order" type="submit">Place order</button>' +
      '<p id="form-error" role="alert"></p></form>';
    document.getElementById('place-order').addEventListener('click', function () {
      var name = document.getElementById('name').value.trim();
      var email = document.getElementById('email').value.trim();
      var address = document.getElementById('address').value.trim();
      if (!name || !email || !address || cart.length === 0) {
        document.getElementById('form-error').textContent = 'Please fill in all fields.';
        return;
      }
      lastOrder = {name: name, items: cart, total: cartTotal()};
      cart = [];
      updateCartCount();
      location.hash = '#/confirmation';
    });
  }

  function renderConfirmation() {
    if (!lastOrder) {
      view.innerHTML = '<p>No order placed.</p>';
      return;
    }
    var items = lastOrder.items.map(function (item) {
      return '<li>' + item.qty + ' × ' + escapeHtml(byId[item.id].name) + '</li>';
    });
    view.innerHTML = '<section class="confirmation"><h1>Order confirmed</h1>' +
      '<p>Thank you, ' + escapeHtml(lastOrder.name) + '!</p><ul>' + items.join('') + '</ul>' +
      '<p>Total: ' + money(lastOrder.total) + '</p></section>';
  }

  function route() {
    var hash = location.hash || '#/';
    var match;
    if ((match = hash.match(/^#\/product\/(\d+)$/)) && byId[match[1]]) {
      renderProduct(byId[match[1]]);
    } else if (hash === '#/cart') {
      renderCart();
    } else if (hash === '#/checkout') {
      renderCheckout();
    } else if (hash === '#/confirmation') {
      renderConfirmation();
    } else if (hash.indexOf('#/search?') === 0) {
      var params = new URLSearchParams(hash.slice('#/search?'.length));
      var q = params.get('q') || '';
      renderList(search(q, params.get('category') || '', params.get('sort') || 'relevance'),
                 'Results for "' + q + '"');
    } else {
      renderList(catalog.products, 'All products');
    }
  }

  document.getElementById('search-btn').addEventListener('click', function () {
    var params = new URLSearchParams({
      q: document.getElementById('search').value,
      category: document.getElementById('category').value,
      sort: document.getElementById('sort').value
    });
    location.hash = '#/search?' + params.toString();
  });

  fetch('catalog.json')
    .then(function (response) { return response.json(); })
    .then(function (data) {
      catalog = data;
      catalog.products.forEach(function (p) { byId[p.id] = p; });
      var select = document.getElementById('category');
      catalog.categories.forEach(function (c) {
        select.insertAdjacentHTML('beforeend', '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>');
      });
      window.addEventListener('hashchange', route);
      route();
    });
})();
