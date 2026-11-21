import ProductCard from "./ProductCard"


const ProductList = ({ products }) => {
  return (
    <div className="bg-white">
      <div className="max-w-2xl mx-auto py-16 px-4 sm:py-24 sm:px-6 lg:max-w-7xl lg:px-8">
        <h2 className="text-2xl font-extrabold text-gray-900 mb-1">
          Products
        </h2>
        <p className="text-sm font-normal text-gray-500 mb-6">
          {products.length === 1 ? "1 product" : `${products.length} products`}
        </p>
        <div className="grid grid-cols-1 gap-y-10 gap-x-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 xl:gap-x-8">
          {
            products.map(product => (
              <ProductCard key={product.node.id} product={product} />
            ))
          }
        </div>
      </div>
    </div>
  )
}

export default ProductList