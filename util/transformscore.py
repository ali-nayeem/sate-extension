
class TransformScore:
    def __init__(self, msa, ml_score):
        self.msa = msa
        self.ml_score = ml_score

    def execute(self):
        transformed_score = self.ml_score

        #calculate simg and simng using a method
        #calculate gap => min.
        #calculate sp score using BLOSUM score matrix
        #transform all score into max. and near 0.9 like MUSCLE
        #calculate the aggregrated score and store into transformed_score

        return transformed_score
