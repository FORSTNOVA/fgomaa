// Explicit PRNG keeps saved deals independent of Dart's Random implementation.
class ReplayRandom {
  int state;
  ReplayRandom(int seed):state=seed==0 ? 0x9e3779b9 : seed & 0xffffffff;
  int nextUint() {
    state ^= (state << 13) & 0xffffffff;state ^= state >> 17;
    state ^= (state << 5) & 0xffffffff;return state &= 0xffffffff;
  }
  int nextInt(int max) {
    if(max<=0)throw StateError('Invalid random range');
    final limit=(0x100000000~/max)*max;
    int value;do {value=nextUint();} while(value>=limit);return value%max;
  }
  double nextDouble()=>nextUint()/0x100000000;
  void shuffle<T>(List<T> list) {
    for(int i=list.length-1;i>0;i--){final j=nextInt(i+1),v=list[i];list[i]=list[j];list[j]=v;}
  }
}
